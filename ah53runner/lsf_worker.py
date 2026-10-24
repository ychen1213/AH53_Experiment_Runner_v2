"""Execute one complete native LSF in one isolated Lumerical session.

The source script owns geometry, all solver runs, analysis and native exports.
This worker never calls ``run`` or changes the source's numerical commands.
"""
from __future__ import annotations

import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import sys
import uuid


_RESERVED = {
    "case.json", "script.lsf", "parameters.json", "status.json", "worker.log",
    "metadata.json", "script_result.json", "model.fsp", "attempts",
    "metadata.json.tmp", "script_result.json.tmp",
}
_INVENTORY_EXCLUDED = _RESERVED - {"model.fsp"}


def _now() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if _is_link(candidate):
            raise ValueError(f"Symbolic links/junctions are not supported: {candidate}")


def _relative_path(value: object, *, input_file: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An artifact/input path must be a nonempty relative path.")
    raw = value.replace("\\", "/")
    path = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (path.is_absolute() or windows.is_absolute() or windows.drive
            or any(part in ("", ".", "..") for part in raw.split("/"))
            or any(":" in part or "\x00" in part for part in path.parts)):
        raise ValueError(f"Unsafe relative path: {value!r}")
    # Reject Win32 aliases such as 'model.fsp.' and 'case.json ', including on
    # POSIX, so an exported configuration has the same meaning on Windows.
    for part in path.parts:
        if (part.endswith((".", " ")) or any(char in part for char in '<>"|?*')
                or any(ord(char) < 32 for char in part)):
            raise ValueError(f"Unsupported relative path component: {value!r}")
        stem = part.split(".", 1)[0].casefold()
        if stem in {"con", "prn", "aux", "nul"} or (
                len(stem) == 4 and stem[:3] in {"com", "lpt"}
                and stem[-1] in "123456789"):
            raise ValueError(f"Reserved Windows filename: {value!r}")
    if input_file and path.parts[0].casefold() in _RESERVED:
        raise ValueError(f"Input file conflicts with a runner-owned path: {value!r}")
    if not input_file and path.parts[0].casefold() in _INVENTORY_EXCLUDED:
        raise ValueError(f"Required output conflicts with runner metadata: {value!r}")
    return path.as_posix()


def _inside(folder: Path, relative: str) -> Path:
    target = folder.joinpath(*PurePosixPath(relative).parts)
    _reject_links(target)
    if not target.resolve().is_relative_to(folder):
        raise ValueError(f"Path escapes the case directory: {relative}")
    return target


def _copy_inputs(folder: Path, execution: dict) -> set[str]:
    inputs = execution.get("input_files", [])
    if not isinstance(inputs, list):
        raise ValueError("execution.input_files must be a list.")
    validated = []
    destinations: set[str] = set()
    for entry in inputs:
        if not isinstance(entry, dict):
            raise ValueError("Each input file must contain source, relative_path and sha256.")
        relative = _relative_path(entry.get("relative_path"), input_file=True)
        key = relative.casefold()
        if key in destinations:
            raise ValueError(f"Duplicate input destination: {relative}")
        if any(key.startswith(old + "/") or old.startswith(key + "/")
               for old in destinations):
            raise ValueError(f"Input file/directory destinations overlap: {relative}")
        destinations.add(key)
        source = Path(entry.get("source", ""))
        if not source.is_absolute():
            raise ValueError(f"Input source must be absolute: {source}")
        _reject_links(source)
        if not source.is_file():
            raise ValueError(f"Input source is not a regular file: {source}")
        if _sha256(source) != entry.get("sha256"):
            raise ValueError(f"Input file changed since planning: {source}")
        target = _inside(folder, relative)
        if target.exists():
            raise ValueError(f"Refusing to overwrite an existing input destination: {target}")
        validated.append((source, target, entry["sha256"]))
    # Validate the entire manifest before copying any dependency.
    for source, target, expected in validated:
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_links(target)
        with source.open("rb") as reader, target.open("xb") as writer:
            shutil.copyfileobj(reader, writer)
        if _sha256(target) != expected or _sha256(source) != expected:
            raise ValueError(f"Input changed while being copied: {source}")
    return destinations


def _load_lumapi(api_dir: str):
    if api_dir:
        folder = Path(api_dir).expanduser().resolve()
        if not (folder / "lumapi.py").is_file():
            raise RuntimeError(f"Cannot find lumapi.py in {folder}.")
        sys.path.insert(0, str(folder))
    try:
        return importlib.import_module("lumapi")
    except ImportError as exc:
        raise RuntimeError("Cannot import lumapi; configure execution.api_dir or --api-dir.") from exc


def _file_record(folder: Path, path: Path) -> dict:
    _reject_links(path)
    if not path.is_file() or not path.resolve().is_relative_to(folder):
        raise ValueError(f"Output is not a regular in-case file: {path}")
    return {"path": path.relative_to(folder).as_posix(),
            "sha256": _sha256(path), "size_bytes": path.stat().st_size}


def _inventory(folder: Path, input_paths: set[str]) -> list[dict]:
    files = []
    for directory, subdirs, names in os.walk(folder, followlinks=False):
        current = Path(directory)
        retained = []
        for name in subdirs:
            child = current / name
            if current == folder and name.casefold() in _INVENTORY_EXCLUDED:
                continue
            if _is_link(child):
                raise ValueError(f"Output directory is a symbolic link/junction: {child}")
            retained.append(name)
        subdirs[:] = retained
        for name in names:
            path = current / name
            relative = path.relative_to(folder).as_posix()
            first = PurePosixPath(relative).parts[0].casefold()
            if first in _INVENTORY_EXCLUDED or relative.casefold() in input_paths:
                continue
            files.append(_file_record(folder, path))
    return sorted(files, key=lambda entry: entry["path"].casefold())


def _dump_atomic(path: Path, payload: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    _reject_links(path)
    _reject_links(temp)
    with temp.open("w", encoding="utf8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    temp.replace(path)


def _marker_completed(value: object) -> bool:
    # lumapi scalar values may be Python numbers or a singleton numpy ndarray;
    # avoid importing a second numerical dependency just to inspect the marker.
    if hasattr(value, "size"):
        if value.size != 1:
            return False
        value = value.item()
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return False


def run_lsf_case(case: dict, result_dir: Path, api_dir: str = "", *,
                 visible: bool = False) -> dict:
    """Run unmodified full-script control flow and record native artifacts.

    ``execution_completed`` asserts only that the complete script returned and
    the requested files exist. It never asserts physical validity, solver
    convergence, or that a script with its own solver switch disabled solved.
    """
    from .lsf_runtime import lsf_fingerprint

    if case.get("execution_kind") != "lsf":
        raise ValueError("The LSF worker requires execution_kind='lsf'.")
    if case.get("solver_fingerprint") != lsf_fingerprint():
        raise RuntimeError("LSF runner source changed after planning; refusing mislabeled execution.")
    script = case.get("script")
    if not isinstance(script, str) or not script.strip():
        raise ValueError("The case must contain a nonempty complete LSF script.")
    if hashlib.sha256(script.encode("utf8")).hexdigest() != case.get("script_sha256"):
        raise ValueError("Case LSF script SHA-256 does not match its frozen manifest.")
    execution = case.get("execution", {})
    if not isinstance(execution, dict) or execution.get("mode", "script") != "script":
        raise ValueError("Only execution.mode='script' is supported; the LSF owns every solver run.")
    folder = Path(result_dir).absolute()
    _reject_links(folder)
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError(f"The planned case directory is missing: {folder}")
    for name in ("metadata.json", "script_result.json", "model.fsp"):
        if (folder / name).exists() or _is_link(folder / name):
            raise ValueError(f"Previous output must be archived before execution: {folder / name}")
    script_path = folder / "script.lsf"
    _reject_links(script_path)
    if not script_path.is_file() or _sha256(script_path) != case["script_sha256"]:
        raise ValueError("Planned script.lsf is missing or differs from the frozen script.")
    required = execution.get("required_outputs", [])
    if not isinstance(required, list):
        raise ValueError("execution.required_outputs must be a relative-path list.")
    required = [_relative_path(value) for value in required]
    if len({value.casefold() for value in required}) != len(required):
        raise ValueError("execution.required_outputs contains duplicate paths.")
    input_paths = _copy_inputs(folder, execution)
    for relative in required:
        if relative.casefold() in input_paths:
            raise ValueError(f"A required output cannot be an input dependency: {relative}")
        if _inside(folder, relative).exists():
            raise ValueError(f"Required output already exists before execution: {relative}")
    started_at = _now()
    api = str(api_dir or execution.get("api_dir", ""))
    lumapi = _load_lumapi(api)
    warnings = [
        "Native LSF controls its own run/save/export operations; script completion is not a physics or convergence certificate.",
        "Native absolute paths or directory changes can write outside this case; only in-case artifacts are inventoried.",
    ]
    version = None
    original_cwd = Path.cwd()
    try:
        os.chdir(folder)
        print("LSF_SESSION_START", json.dumps({"case": case.get("id"), "api_dir": api,
              "execution_mode": "script", "required_outputs": required}, ensure_ascii=False), flush=True)
        with lumapi.FDTD(hide=not visible) as session:
            session.cd(folder.as_posix())
            try:
                version = str(session.version()).strip()
            except Exception as exc:
                raise RuntimeError("Cannot record the installed Lumerical version.") from exc
            if not version:
                raise RuntimeError("Lumerical returned an empty installed-version string.")
            token = uuid.uuid4().hex
            source_var = "AH53_source_" + token
            error_var = "AH53_error_" + token
            marker_var = "AH53_completed_" + token
            # The unique marker follows the complete original text. Missing it
            # means break/exit/another premature end and must never count as DONE.
            session.putv(source_var, script + "\n" + marker_var + " = 1;\n")
            session.eval("try { eval(" + source_var + "); } catch(" + error_var + ");")
            try:
                native_error = session.getv(error_var)
            except Exception:
                native_error = ""
            if native_error is not None and str(native_error).strip():
                raise RuntimeError("Native LSF error: " + str(native_error))
            try:
                completed = _marker_completed(session.getv(marker_var))
            except Exception:
                completed = False
            if not completed:
                raise RuntimeError("The full LSF did not reach its completion marker (break/exit or interrupted execution).")
            session.save((folder / "model.fsp").as_posix())
        print("LSF_SESSION_CLOSED", flush=True)
    finally:
        os.chdir(original_cwd)
    for relative in ["model.fsp", *required]:
        target = _inside(folder, relative)
        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError(f"Native LSF did not produce a nonempty required output: {relative}")
    files = _inventory(folder, input_paths)
    provenance = {key: case[key] for key in
                  ("digest", "solver_fingerprint", "script_sha256", "source_sha256")}
    result = {"schema_version": "lsf-script-result-1.0", "execution_kind": "lsf",
              "execution_completed": True, "completion_kind": "script_completed",
              "execution_mode": "script", **provenance,
              "lumerical_version": version, "api_dir": api,
              "started_at": started_at, "completed_at": _now(),
              "required_outputs": required, "files": files, "warnings": warnings}
    result_path = folder / "script_result.json"
    _dump_atomic(result_path, result)
    metadata = {"schema_version": "lsf-artifacts-1.0", "execution_kind": "lsf",
                "execution_mode": "script", **provenance,
                "lumerical_version": version,
                "files": [*files, _file_record(folder, result_path)]}
    try:
        _dump_atomic(folder / "metadata.json", metadata)
    except BaseException:
        result_path.unlink(missing_ok=True)
        raise
    return result
