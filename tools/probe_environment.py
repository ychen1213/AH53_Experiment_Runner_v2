"""Record local runtime/license/FDTD resources without running a simulation.

Invoke with the intended Python interpreter. No resource property is changed.
The optional API probe owns and closes one independent hidden CAD session.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback


def command_record(args: list[str], timeout: int = 30) -> dict:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                errors="replace", timeout=timeout)
        # lmstat may print the license-file location; never read/copy its contents.
        output = "\n".join(line for line in (result.stdout + result.stderr).splitlines()
                           if "License file(s)" not in line)
        return {"command": args, "returncode": result.returncode, "output": output}
    except Exception as error:
        return {"command": args, "error": repr(error)}


def json_value(value):
    if hasattr(value, "tolist"):
        return value.tolist()
    return value


def memory_info() -> dict:
    if os.name != "nt":
        return {}
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load_percent", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_physical", "available_physical", "total_page_file",
                "available_page_file", "total_virtual", "available_virtual",
                "available_extended_virtual")]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return {name: getattr(status, name) for name in
                ("load_percent", "total_physical", "available_physical")}
    return {"error": "GlobalMemoryStatusEx failed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-path", default="D:/Ansys Inc/v252/Lumerical/api/python")
    parser.add_argument("--installation", default="D:/Ansys Inc/v252/Lumerical")
    parser.add_argument("--lmutil", default="D:/Ansys Inc/Shared Files/fnp/winx64/lmutil.exe")
    parser.add_argument("--license-server", default="1055@localhost")
    parser.add_argument("--output", default="local_validation/environment.json")
    parser.add_argument("--skip-api", action="store_true")
    args = parser.parse_args()
    record = {
        "captured_at": datetime.now().astimezone().isoformat(),
        "requested_lumerical_version": "2023 R2.1",
        "authorized_version": "2025 R2",
        "version_difference": "User authorized installed 2025 R2; 2023 R2.1 compatibility is not validated.",
        "python": {"executable": sys.executable, "version": sys.version},
        "system": {"platform": platform.platform(), "logical_cpu_count": os.cpu_count(),
                   "memory": memory_info()},
        "api_path": str(Path(args.api_path).resolve()),
        "simulation_executed": False,
        "resource_properties_modified": False,
        "sources": ["https://optics.ansys.com/hc/en-us/articles/360034931353-getresource-Script-command"],
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2,
                                     default=json_value) + "\n", encoding="utf-8")

    for name in ("numpy", "matplotlib"):
        try:
            module = importlib.import_module(name)
            record[ name ] = {"version": module.__version__, "path": module.__file__}
        except Exception as error:
            record[name] = {"error": repr(error)}
    record["engine_version"] = command_record([
        str(Path(args.installation) / "bin" / "fdtd-engine.exe"), "-v"])
    record["license_status"] = command_record([
        str(Path(args.lmutil)), "lmstat", "-c", args.license_server])
    license_text = record["license_status"].get("output", "")
    record["license_status"]["server_up"] = (
        "license server UP" in license_text and "ansyslmd: UP" in license_text)
    save()
    if not args.skip_api:
        session = None
        record["fdtd_api"] = {"session_hidden": True, "status": "opening"}
        save()
        try:
            sys.path.insert(0, args.api_path)
            import lumapi
            record["fdtd_api"]["module_path"] = lumapi.__file__
            session = lumapi.FDTD(hide=True)
            record["fdtd_api"]["status"] = "opened"
            record["fdtd_api"]["version"] = session.version()
            resources = []
            count = int(session.getresource("FDTD"))
            for index in range(1, count + 1):
                available = session.getresource("FDTD", index)
                item = {"index": index, "available_properties": available, "properties": {}}
                for prop in ("name", "active", "hostname", "processes", "threads",
                             "capacity", "job launching preset", "mpi executable", "device type"):
                    if prop not in available.splitlines():
                        continue
                    try:
                        item["properties"][prop] = json_value(session.getresource("FDTD", index, prop))
                    except Exception as error:
                        item.setdefault("unsupported_properties", {})[prop] = str(error)
                resources.append(item)
            record["fdtd_api"]["resource_count"] = count
            record["fdtd_api"]["resources"] = resources
            record["fdtd_api"]["status"] = "ok"
        except Exception as error:
            record["fdtd_api"]["status"] = "failed"
            record["fdtd_api"]["error"] = repr(error)
            record["fdtd_api"]["traceback"] = traceback.format_exc()
        finally:
            if session is not None:
                try:
                    session.close()
                    record["fdtd_api"]["session_closed"] = True
                except Exception as error:
                    record["fdtd_api"]["close_error"] = repr(error)
            save()
    print(json.dumps(record, ensure_ascii=False, indent=2, default=json_value))
    failed = (record.get("fdtd_api", {}).get("status") == "failed"
              or any("error" in record[name] for name in ("numpy", "matplotlib"))
              or record["engine_version"].get("returncode") != 0
              or not record["license_status"]["server_up"])
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
