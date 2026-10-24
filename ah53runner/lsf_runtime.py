"""Plan and verify complete LSF tasks without importing the solver API.

The source LSF remains the modeling authority. This module freezes expanded
scripts and explicit file dependencies; it never adds a second solver run.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

from .lsf_config import expand_bundle, inspect_lsf

SCHEMA = 'lsf-sweep-1.0'
ROOT = Path(__file__).resolve().parent.parent
_RESERVED = {
    'case.json', 'script.lsf', 'parameters.json', 'status.json', 'metadata.json',
    'script_result.json', 'worker.log', 'model.fsp', 'attempts', '.queue.lock',
    'manifest.json', 'report.html', 'summary.csv',
    'metadata.json.tmp', 'script_result.json.tmp', 'case.json.tmp',
    'parameters.json.tmp', 'status.json.tmp',
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _json(path):
    def nonfinite(value):
        raise ValueError(f'Nonfinite JSON number: {value}')
    try:
        result = json.loads(Path(path).read_text(encoding='utf-8-sig'),
                            parse_constant=nonfinite)
    except (OSError, ValueError) as exc:
        raise ValueError(f'{path}: {exc}') from exc
    _require(isinstance(result, dict), f'{path}: JSON root must be an object')
    return result


def _write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                    allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _relative(value, label, reserved=False):
    _require(isinstance(value, str) and value.strip() and '\x00' not in value,
             f'{label}: expected a nonempty relative file path')
    value = value.replace('\\', '/')
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    _require(not posix.is_absolute() and not windows.drive and not windows.root,
             f'{label}: absolute paths are not allowed: {value}')
    raw_parts = value.split('/')
    _require(all(part and part not in {'.', '..'} for part in raw_parts),
             f'{label}: empty, dot, and parent path components are not allowed')
    forbidden = re.compile(r'[<>:"|?*\x00-\x1f]')
    for part in raw_parts:
        _require(not forbidden.search(part) and part.rstrip(' .') == part,
                 f'{label}: unsupported Windows filename component: {part}')
        _require(not re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part),
                 f'{label}: reserved Windows filename: {part}')
    if reserved:
        _require(raw_parts[0].casefold() not in _RESERVED,
                 f'{label}: path conflicts with runner files: {value}')
    return posix.as_posix()


def _within(folder, relative):
    candidate = folder / relative
    for part in (candidate, *candidate.parents):
        _require(not part.is_symlink()
                 and not (hasattr(part, 'is_junction') and part.is_junction()),
                 f'Symbolic links/junctions are not supported: {part}')
    path = candidate.resolve()
    _require(path.is_relative_to(folder.resolve()),
             f'Artifact path escapes case directory: {relative}')
    return path


def _default_api():
    candidates = [Path(sys.executable).resolve().parent.parent / 'api' / 'python']
    local = ROOT / 'configs' / 'local_runtime.json'
    if local.is_file():
        settings = _json(local)
        configured = settings.get('lumerical_api_python')
        if isinstance(configured, str) and configured.strip():
            candidate = Path(configured).expanduser()
            candidates.append(candidate if candidate.is_absolute() else local.parent / candidate)
        executable = settings.get('python_executable')
        if isinstance(executable, str) and executable.strip():
            executable = Path(executable).expanduser()
            if not executable.is_absolute():
                executable = local.parent / executable
            candidates.append(executable.parent.parent / 'api' / 'python')
    return str(next((candidate.resolve() for candidate in candidates
                     if (candidate / 'lumapi.py').is_file()), candidates[0].resolve()))


def is_lsf_config(cfg):
    return isinstance(cfg, dict) and cfg.get('schema_version') == SCHEMA


def _normalise(cfg, config_path):
    _require(is_lsf_config(cfg), f'Expected schema_version {SCHEMA}')
    result = copy.deepcopy(cfg)
    output = result.get('output_dir', 'output_lsf_' + config_path.stem)
    _require(isinstance(output, str) and output.strip() and '\x00' not in output,
             'output_dir must be a nonempty path string')
    output_path = Path(output).expanduser()
    if not output_path.is_absolute():
        output_path = config_path.parent / output_path
    result['output_dir'] = str(output_path.resolve())
    minutes = result.get('max_case_minutes', 90)
    _require(isinstance(minutes, (int, float)) and not isinstance(minutes, bool)
             and math.isfinite(minutes) and minutes >= 1,
             'max_case_minutes must be a finite number >= 1')
    result['max_case_minutes'] = minutes
    api = result.get('lumerical_api_python') or _default_api()
    _require(isinstance(api, str) and api.strip() and '\x00' not in api,
             'lumerical_api_python must be a nonempty directory path')
    api_path = Path(api).expanduser()
    if not api_path.is_absolute():
        api_path = config_path.parent / api_path
    result['lumerical_api_python'] = str(api_path.resolve())
    execution = result.get('execution', {})
    _require(isinstance(execution, dict), 'execution must be an object')
    _require(execution.get('mode', 'script') == 'script',
             'Only execution.mode="script" is supported; solver commands belong in the LSF')
    raw_inputs = execution.get('input_files', [])
    _require(isinstance(raw_inputs, list), 'execution.input_files must be a list')
    inputs, destinations = [], []
    for index, entry in enumerate(raw_inputs):
        label = f'execution.input_files[{index}]'
        _require(isinstance(entry, dict), f'{label} must be an object')
        source = entry.get('source')
        _require(isinstance(source, str) and source.strip() and '\x00' not in source,
                 f'{label}.source must be a file path')
        source_path = Path(source).expanduser()
        if not source_path.is_absolute():
            source_path = config_path.parent / source_path
        source_path = source_path.resolve()
        _require(source_path.is_file(), f'{label}: missing input file: {source_path}')
        relative = _relative(entry.get('relative_path'), label + '.relative_path', reserved=True)
        folded = relative.casefold()
        _require(all(folded != old and not folded.startswith(old + '/')
                     and not old.startswith(folded + '/') for old in destinations),
                 f'{label}: duplicate or overlapping input destination: {relative}')
        destinations.append(folded)
        inputs.append({'source': str(source_path), 'relative_path': relative,
                       'sha256': _file_hash(source_path)})
    required = execution.get('required_outputs', [])
    _require(isinstance(required, list), 'execution.required_outputs must be a list')
    outputs, seen = [], set()
    for entry in required:
        relative = _relative(entry, 'execution.required_outputs')
        folded = relative.casefold()
        _require(folded not in seen, f'Duplicate required output: {relative}')
        first = relative.split('/')[0].casefold()
        _require(first not in _RESERVED or relative == 'model.fsp',
                 f'Required output conflicts with runner metadata: {relative}')
        _require(all(folded != old and not folded.startswith(old + '/')
                     and not old.startswith(folded + '/') for old in destinations),
                 f'Required output conflicts with an input file: {relative}')
        outputs.append(relative)
        seen.add(folded)
    result['execution'] = {'mode': 'script', 'api_dir': result['lumerical_api_python'],
                           'input_files': inputs, 'required_outputs': outputs}
    return result


def load_lsf_config(path):
    path = Path(path).expanduser().resolve()
    cfg = _normalise(_json(path), path)
    expand_bundle(cfg)  # Verify all source anchors before preparing any files.
    return cfg, path


def lsf_fingerprint():
    digest = hashlib.sha256()
    for name in ('ah53runner/lsf_config.py', 'ah53runner/lsf_runtime.py',
                 'ah53runner/lsf_worker.py', 'run_lsf_worker.py'):
        digest.update(name.encode('utf-8'))
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()[:16]


def plan_lsf(cfg, path):
    path = Path(path).expanduser().resolve()
    normal = _normalise(cfg, path)
    expanded = expand_bundle(normal)
    inspection = inspect_lsf(normal['source']['script'])
    parameter_id = normal['sweep']['parameter_id']
    parameter = next(item['label'] for item in inspection['parameters']
                     if item['id'] == parameter_id)
    group = re.sub(r'[^a-zA-Z0-9_.-]+', '_', normal['name'])[:64].strip('.') or 'lsf_scan'
    fingerprint = lsf_fingerprint()
    cases = []
    for item in expanded:
        identity = {'source_sha256': normal['source']['sha256'],
                    'script': item['script'], 'execution': normal['execution']}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':'), allow_nan=False)
                                .encode('utf-8')).hexdigest()
        value_text = re.sub(r'[^a-zA-Z0-9_.-]+', '_', str(item['value']))[:32]
        case = {'id': f'{group}_{item["index"]:03}_{value_text}_{digest[:12]}',
                'digest': digest, 'group': group, 'index': item['index'],
                'parameter': parameter, 'parameter_id': parameter_id,
                'value': item['value'], 'execution_kind': 'lsf',
                'script': item['script'], 'script_sha256': _text_hash(item['script']),
                'source_sha256': normal['source']['sha256'],
                'source_filename': normal['source']['filename'],
                'dimension': inspection['dimension'], 'solver_fingerprint': fingerprint,
                'execution': copy.deepcopy(normal['execution']),
                'parameter_changes': item['changes'],
                'override_insertion_offset': item['override_insertion_offset'],
                'warnings': inspection['warnings']}
        cases.append(case)
    return cases


def prepare_lsf(path, cfg, cases, archive_callback):
    path = Path(path).expanduser().resolve()
    output = Path(cfg.get('output_dir', 'output_lsf_' + path.stem)).expanduser()
    if not output.is_absolute():
        output = path.parent / output
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    current_fingerprint = lsf_fingerprint()
    for case in cases:
        _require(case.get('execution_kind') == 'lsf', 'Expected an LSF execution case')
        _require(case.get('solver_fingerprint') == current_fingerprint,
                 'LSF implementation changed after planning; plan again')
        folder = _within(output, _relative(case['id'], 'case ID'))
        folder.mkdir(exist_ok=True)
        casefile = folder / 'case.json'
        previous = None
        if casefile.exists():
            try:
                previous = _json(casefile)
            except ValueError:
                pass
            _require(previous is None or previous.get('digest') == case['digest'],
                     f'Case ID collision: {folder}')
        script_path = folder / 'script.lsf'
        stale = (casefile.exists() and (previous is None
                 or previous.get('solver_fingerprint') != current_fingerprint
                 or previous.get('execution') != case['execution']
                 or previous.get('script_sha256') != case['script_sha256']
                 or not script_path.is_file()
                 or _file_hash(script_path) != case['script_sha256']))
        if stale or (not casefile.exists() and any(folder.iterdir())):
            archived = archive_callback(folder)
            if archived:
                _write_json(folder / 'status.json', {
                    'status': 'INCOMPLETE', 'digest': case['digest'],
                    'solver_fingerprint': current_fingerprint,
                    'error': 'LSF execution inputs or implementation changed; previous attempt retained',
                    'previous_attempt': str(Path(archived).relative_to(folder))})
        _write_json(casefile, case)
        # Write exact UTF-8 bytes: newline translation would invalidate source hashes.
        script_path.write_bytes(case['script'].encode('utf-8'))
        _write_json(folder / 'parameters.json', {
            'schema_version': SCHEMA, 'digest': case['digest'],
            'source_sha256': case['source_sha256'], 'script_sha256': case['script_sha256'],
            'changes': case['parameter_changes'],
            'override_insertion_offset': case['override_insertion_offset']})
    _write_json(output / 'manifest.json', {
        'schema_version': SCHEMA, 'execution_kind': 'lsf', 'config_file': str(path),
        'cases': [{key: case[key] for key in (
            'id', 'digest', 'group', 'index', 'parameter', 'value',
            'solver_fingerprint', 'script_sha256', 'source_sha256')} for case in cases]})
    return output


def _file_records(folder, records, label):
    _require(isinstance(records, list), f'{label}: expected an artifact list')
    by_path, folded_paths = {}, set()
    for record in records:
        _require(isinstance(record, dict), f'{label}: invalid artifact record')
        relative = _relative(record.get('path'), label + '.path')
        _require(relative.split('/')[0].casefold() not in _RESERVED
                 or relative in {'model.fsp', 'script_result.json'},
                 f'{label}: runner metadata cannot be a native output: {relative}')
        _require(relative.casefold() not in folded_paths, f'{label}: duplicate artifact {relative}')
        folded_paths.add(relative.casefold())
        path = _within(folder, relative)
        _require(path.is_file(), f'{label}: missing artifact {relative}')
        size = record.get('size_bytes')
        _require(type(size) is int and size >= 0 and path.stat().st_size == size,
                 f'{label}: size mismatch for {relative}')
        _require(record.get('sha256') == _file_hash(path),
                 f'{label}: checksum mismatch for {relative}')
        by_path[relative] = record
    return by_path


def load_lsf_artifacts(folder, case, require_done=True):
    folder = _within(Path(folder).absolute(), '')
    _require(case.get('execution_kind') == 'lsf', 'Expected an LSF case')
    _require(case.get('solver_fingerprint') == lsf_fingerprint(),
             'LSF implementation fingerprint changed; previous outputs cannot be reused')
    frozen = _json(folder / 'case.json')
    _require(frozen == case, 'case.json differs from the planned LSF case')
    _require(_text_hash(case['script']) == case['script_sha256'],
             'Case script SHA-256 does not match its text')
    _require((folder / 'script.lsf').is_file()
             and _file_hash(folder / 'script.lsf') == case['script_sha256'],
             'script.lsf checksum mismatch')
    for entry in case['execution'].get('input_files', []):
        relative = _relative(entry['relative_path'], 'input dependency', reserved=True)
        staged = _within(folder, relative)
        _require(staged.is_file() and _file_hash(staged) == entry['sha256'],
                 f'Staged input dependency is missing or changed: {relative}')
    if require_done:
        status = _json(folder / 'status.json')
        _require(status.get('status') == 'DONE', 'LSF case is not DONE')
        _require(status.get('digest') == case['digest']
                 and status.get('solver_fingerprint') == case['solver_fingerprint'],
                 'status.json belongs to another LSF execution')
    metadata = _json(folder / 'metadata.json')
    result = _json(folder / 'script_result.json')
    _require(metadata.get('schema_version') == 'lsf-artifacts-1.0'
             and metadata.get('execution_kind') == 'lsf', 'Unsupported LSF artifact metadata')
    _require(result.get('schema_version') == 'lsf-script-result-1.0'
             and result.get('execution_completed') is True
             and result.get('completion_kind') == 'script_completed',
             'LSF execution did not produce a completion record')
    for label, data in (('metadata.json', metadata), ('script_result.json', result)):
        for field in ('digest', 'solver_fingerprint', 'script_sha256', 'source_sha256'):
            _require(data.get(field) == case[field], f'{label}: inconsistent {field}')
        _require(data.get('execution_mode') == 'script', f'{label}: unexpected execution mode')
        _require(isinstance(data.get('lumerical_version'), str)
                 and data['lumerical_version'].strip(), f'{label}: missing Lumerical version')
    _require(metadata['lumerical_version'] == result['lumerical_version'],
             'Inconsistent Lumerical version records')
    _require(result.get('required_outputs') == case['execution'].get('required_outputs', []),
             'Required native outputs differ from the planned case')
    result_files = _file_records(folder, result.get('files'), 'script_result.json')
    metadata_files = _file_records(folder, metadata.get('files'), 'metadata.json')
    _require('model.fsp' in result_files and result_files['model.fsp']['size_bytes'] > 0,
             'Missing or empty saved Lumerical model.fsp')
    _require('script_result.json' in metadata_files, 'metadata.json does not fingerprint script_result.json')
    _require({key: value for key, value in metadata_files.items() if key != 'script_result.json'}
             == result_files, 'Native artifact inventories disagree')
    for relative in case['execution'].get('required_outputs', []):
        _require(relative in result_files and result_files[relative]['size_bytes'] > 0,
                 f'Missing or empty required native output: {relative}')
    parameters = _json(folder / 'parameters.json')
    _require(parameters.get('digest') == case['digest']
             and parameters.get('changes') == case['parameter_changes']
             and parameters.get('script_sha256') == case['script_sha256']
             and parameters.get('source_sha256') == case['source_sha256']
             and parameters.get('override_insertion_offset') == case['override_insertion_offset'],
             'parameters.json differs from the planned parameter changes')
    return {'metadata': metadata, 'script_result': result, 'files': metadata['files']}
