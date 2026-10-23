"""Independently audit CSV/HTML against current real solver artifacts.

Reads simulation outputs only. The sole written file is a separate audit JSON;
no solver session, status repair, report regeneration or figure edits occur.
"""
from __future__ import annotations

import argparse
import copy
import csv
import datetime
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
import zipfile

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ah53runner.artifacts import load_artifacts, solver_fingerprint, strict_json_loads
from ah53runner.planning import load_config, plan

COMPONENTS = {'rss': (0, 0), 'rsp': (0, 1), 'rps': (1, 0), 'rpp': (1, 1)}
FIXED_COORDINATES = ('source_y_um', 'reflection_y_um', 'transmission_y_um',
                     'incident1_y_um', 'incident2_y_um', 'reference_y_um',
                     'field_y_min_um', 'field_y_max_um')


class AuditChecks:
    def __init__(self):
        self.count = 0
        self.error_count = 0
        self.errors = []

    def require(self, condition, description):
        self.count += 1
        if not condition:
            self.error_count += 1
            if len(self.errors) < 100:
                self.errors.append(description)


class Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == 'img':
            self.sources.append(dict(attrs).get('src', ''))


def _numeric_matches(value, expected, *, phase=False):
    try:
        actual = float(value)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(actual):
        return False
    if phase:
        error = math.degrees(math.atan2(math.sin(math.radians(actual - expected)),
                                        math.cos(math.radians(actual - expected))))
        return abs(error) <= 1e-8
    return math.isclose(actual, float(expected), rel_tol=1e-10, abs_tol=1e-12)


def _masked_model(model, dotted):
    """Independent single-variable comparison; does not call planning.apply_patch."""
    value = copy.deepcopy(model)
    target = value
    keys = dotted.split('.')
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(keys[-1])] = '__AUDIT_SWEEP_VALUE__'
    else:
        target[keys[-1]] = '__AUDIT_SWEEP_VALUE__'
    return value


def audit_coefficient_rows(rows, fieldnames, valid, config, checks):
    """Calculate expected CSV values directly from Jones arrays, independently."""
    expected_columns = {'case_id', 'group', 'parameter', 'value', 'm', 'theta_deg', 'eta_s', 'eta_p'}
    for name in COMPONENTS:
        expected_columns.update(name + suffix for suffix in ('_real', '_imag', '_abs', '_phase_deg', '_source'))
    checks.require(len(fieldnames) == len(set(fieldnames)) and set(fieldnames) == expected_columns,
                   'CSV columns are duplicated, missing, or contain unaudited extra fields.')
    expected_rows = sum(len(record['result']['orders']) for record in valid.values())
    checks.require(len(rows) == expected_rows,
                   f'CSV row count {len(rows)} differs from valid DONE order count {expected_rows}.')
    seen = set()
    phase_counts = {'blank_below_floor': 0, 'numeric_above_floor': 0}
    for line, row in enumerate(rows, 2):
        checks.require(set(row) == expected_columns,
                       f'CSV line {line}: missing or extra row fields are not auditable.')
        identity = row.get('case_id')
        if identity not in valid:
            checks.require(False, f'CSV line {line}: case is outside the current valid DONE set: {identity}')
            continue
        try:
            order = int(row['m'])
        except (KeyError, TypeError, ValueError):
            checks.require(False, f'CSV line {line}: invalid integer order.')
            continue
        record = valid[identity]
        case, result, metadata = record['case'], record['result'], record['metadata']
        key = (identity, order)
        checks.require(key not in seen, f'CSV line {line}: duplicate case/order {key}.')
        seen.add(key)
        indices = np.flatnonzero(result['orders'] == order)
        if len(indices) != 1:
            checks.require(False, f'CSV line {line}: order {order} absent from result.npz.')
            continue
        index = int(indices[0])
        for column in ('group', 'parameter'):
            checks.require(row.get(column) == case[column], f'CSV line {line}: {column} differs from frozen case.')
        checks.require(_numeric_matches(row.get('value'), case['value']), f'CSV line {line}: sweep value mismatch.')
        u = order * float(result['lambda_nm'][0]) / float(case['model']['period_nm'])
        angle = math.degrees(math.asin(u))
        cosine = math.sqrt(1 - u*u)
        checks.require(_numeric_matches(row.get('theta_deg'), angle), f'CSV line {line}: angle mismatch.')
        for j, pol in enumerate(('s', 'p')):
            eta = cosine * sum(abs(complex(result['r_E'][index, out, j]))**2 for out in (0, 1))
            checks.require(_numeric_matches(row.get('eta_' + pol), eta), f'CSV line {line}: eta_{pol} mismatch.')
        for name, (out, incoming) in COMPONENTS.items():
            z = complex(result['r_E'][index, out, incoming])
            for suffix, expected in (('_real', z.real), ('_imag', z.imag), ('_abs', abs(z))):
                checks.require(_numeric_matches(row.get(name + suffix), expected),
                               f'CSV line {line}: {name + suffix} differs from result.npz.')
            amplitude_max = max(abs(complex(a)) for a in result['r_E'][:, out, incoming])
            floor = max(float(config.get('phase_min_amplitude', 1e-8)),
                        float(config.get('phase_relative_amplitude_floor', 1e-3)) * amplitude_max)
            phase_value = row.get(name + '_phase_deg')
            blank = phase_value is None or not str(phase_value).strip()
            if abs(z) <= floor:
                checks.require(blank, f'CSV line {line}: {name} phase is reported at/below amplitude floor {floor}.')
                phase_counts['blank_below_floor'] += int(blank)
            else:
                expected_phase = math.degrees(math.atan2(z.imag, z.real))
                checks.require(not blank and _numeric_matches(phase_value, expected_phase, phase=True),
                               f'CSV line {line}: {name} phase missing or inconsistent with complex coefficient.')
                phase_counts['numeric_above_floor'] += int(not blank)
            source = 'fdtd_fields' if out == incoming else 'analytic_by_2d_symmetry'
            checks.require(row.get(name + '_source') == source,
                           f'CSV line {line}: {name} source must explicitly be {source}.')
            if out != incoming:
                checks.require(metadata.get('cross_terms_source') == source and abs(z) <= 1e-15,
                               f'CSV line {line}: theoretical cross-term source is missing or coefficient is nonzero.')
    expected_keys = {(identity, int(order)) for identity, record in valid.items()
                     for order in record['result']['orders']}
    checks.require(seen == expected_keys, 'CSV case/order coverage differs from all current valid DONE orders.')
    return {'expected_rows': expected_rows, 'actual_rows': len(rows), 'phase_counts': phase_counts}


def audit_html_images(output, valid, checks):
    parser = Images()
    parser.feed((output / 'report.html').read_text(encoding='utf8'))
    allowed_nearfields = {}
    groups = set()
    for identity, record in valid.items():
        groups.add(record['case']['group'])
        for pol, field in record.get('nearfields', {}).items():
            if float(np.max(field['E2'])) > 0:
                allowed_nearfields[f'field_{identity}_{pol}.png'] = identity
    referenced = set()
    for source in parser.sources:
        url = urlsplit(source)
        checks.require(not url.scheme and not url.netloc, f'HTML image is not a local audit artifact: {source}')
        if url.scheme or url.netloc:
            continue
        path = (output / unquote(url.path)).resolve()
        checks.require(path.is_relative_to(output.resolve()), f'HTML image escapes output directory: {source}')
        checks.require(path.parent == (output / 'figures').resolve(),
                       f'HTML image is outside the current figures directory: {source}')
        checks.require(path.is_file() and path.stat().st_size > 0, f'HTML image missing or empty: {source}')
        if path.name.startswith('field_'):
            checks.require(path.name in allowed_nearfields, f'HTML references a stale or invalid-case nearfield: {source}')
            checks.require(path.name not in referenced, f'HTML repeats nearfield image: {source}')
            referenced.add(path.name)
        else:
            suffixes = ('rss_amplitude', 'rpp_amplitude', 'plus_minus_phase',
                        'reflected_power', 'complex_plane', 'complex_convergence')
            allowed = {f'{group}_{suffix}.png' for group in groups for suffix in suffixes}
            checks.require(path.name in allowed, f'HTML aggregate image is outside current valid groups: {source}')
    checks.require(referenced == set(allowed_nearfields),
                   'HTML nearfield references do not cover exactly the current valid nonzero nearfields.')
    return {'image_count': len(parser.sources), 'nearfield_count': len(referenced),
            'nearfield_cases': sorted(set(allowed_nearfields.values()))}


def audit_raw_monitor_coordinates(folder, case, checks):
    positions = {'R': 'reflection_y_um', 'T': 'transmission_y_um',
                 'INC1': 'incident1_y_um', 'INC2': 'incident2_y_um'}
    period = float(case['model']['period_nm']) * 1e-9
    coordinates = {}
    for stage in ('reference_s', 'reference_p', 'structure_s', 'structure_p'):
        with np.load(folder / f'raw_{stage}.npz', allow_pickle=False) as raw:
            coordinates[stage] = {}
            for monitor, key in positions.items():
                y = np.asarray(raw[monitor + '__y']).reshape(-1)
                x = np.asarray(raw[monitor + '__x']).reshape(-1)
                expected_y = float(case['model']['sim'][key]) * 1e-6
                checks.require(len(y) == 1 and np.isfinite(y).all()
                               and abs(float(y[0]) - expected_y) <= 1e-12,
                               f'{case["id"]}/{stage}/{monitor}: raw y differs from fixed configuration.')
                checks.require(len(x) >= 2 and np.isfinite(x).all()
                               and abs(float(x[0]) + period/2) <= 1e-12
                               and abs(float(x[-1]) - period/2) <= 1e-12,
                               f'{case["id"]}/{stage}/{monitor}: raw X span differs from fixed period.')
                coordinates[stage][monitor] = {'y_m': float(y[0]),
                    'x_min_m': float(x[0]), 'x_max_m': float(x[-1])}
    return coordinates


def audit_outputs(config, config_path, audit_path, require_all=False):
    """Audit only; source artifacts are never changed."""
    checks = AuditChecks()
    path = Path(config_path).resolve()
    output = Path(config['output_dir']).expanduser()
    if not output.is_absolute():
        output = path.parent / output
    output = output.resolve()
    cases = plan(config)
    fingerprint = solver_fingerprint()
    valid, skipped, provenance, coordinate_evidence = {}, [], [], {}
    group_models = {}
    baseline_coordinates = {key: config['base_model']['sim'][key] for key in FIXED_COORDINATES}
    environment_path = ROOT / 'local_validation' / 'environment.json'
    environment = strict_json_loads(environment_path.read_text(encoding='utf8')) if environment_path.exists() else {}
    expected_version = environment.get('fdtd_api', {}).get('version')
    for case in cases:
        case['solver_fingerprint'] = fingerprint
        model = case['model']
        checks.require({key: model['sim'][key] for key in FIXED_COORDINATES} == baseline_coordinates,
                       f'{case["id"]}: source/monitor/reference coordinates moved from the fixed baseline.')
        checks.require(model['period_nm'] == config['base_model']['period_nm'],
                       f'{case["id"]}: periodic/source/monitor X span moved from the fixed baseline.')
        normalized = _masked_model(model, case['parameter'])
        if case['group'] in group_models:
            checks.require(normalized == group_models[case['group']],
                           f'{case["id"]}: another model value changed within the single-variable group.')
        else:
            group_models[case['group']] = normalized
        folder = output / case['id']
        try:
            status = strict_json_loads((folder / 'status.json').read_text(encoding='utf8'))
            if status.get('status') != 'DONE':
                skipped.append({'case_id': case['id'], 'status': status.get('status', 'UNKNOWN')})
                continue
            verified = load_artifacts(folder, case)
            metadata = verified['metadata']
            version = metadata.get('lumerical_version')
            checks.require(isinstance(version, str) and bool(re.fullmatch(r'\d+\.\d+\.\d+', version)),
                           f'{case["id"]}: actual Lumerical metadata version is missing or invalid.')
            if expected_version is not None:
                checks.require(version == expected_version,
                               f'{case["id"]}: actual engine version differs from the verified local environment.')
            checks.require(metadata.get('reference_y_um') == model['sim']['reference_y_um'],
                           f'{case["id"]}: metadata reference plane differs from frozen configuration.')
            checks.require(metadata.get('physical_model') == '2D lossless dielectric XY',
                           f'{case["id"]}: source labels require the supported physical model.')
            verified['case'] = case
            valid[case['id']] = verified
            coordinate_evidence[case['id']] = audit_raw_monitor_coordinates(folder, case, checks)
            provenance.append({'case_id': case['id'], 'digest': metadata['digest'],
                'solver_fingerprint': metadata['solver_fingerprint'], 'result_sha256': metadata['result_sha256'],
                'lumerical_version': version, 'jones_phase_status': metadata['jones_phase_status']})
        except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError, zipfile.BadZipFile) as exc:
            if not (folder / 'status.json').exists():
                skipped.append({'case_id': case['id'], 'status': 'MISSING'})
            else:
                checks.require(False, f'{case["id"]}: current DONE artifacts refused: {type(exc).__name__}: {exc}')
    checks.require(bool(valid), 'No complete current real DONE cases are available to audit.')
    if require_all:
        checks.require(len(valid) == len(cases), f'Queue is incomplete: {len(valid)}/{len(cases)} valid DONE cases.')
    csv_summary = {}
    image_summary = {}
    try:
        with (output / 'coefficients_long.csv').open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            csv_summary = audit_coefficient_rows(rows, reader.fieldnames or [], valid, config, checks)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        checks.require(False, f'CSV audit refused: {type(exc).__name__}: {exc}')
    try:
        image_summary = audit_html_images(output, valid, checks)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        checks.require(False, f'HTML audit refused: {type(exc).__name__}: {exc}')
    report = {'status': 'PASS' if checks.error_count == 0 else 'FAIL',
        'evaluated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds'),
        'config': str(path), 'output_dir': str(output), 'solver_fingerprint': fingerprint,
        'total_cases': len(cases), 'valid_done_cases': len(valid), 'complete_queue': len(valid) == len(cases),
        'require_all': require_all, 'check_count': checks.count, 'error_count': checks.error_count,
        'errors': checks.errors, 'skipped_cases': skipped, 'csv': csv_summary,
        'html': image_summary, 'provenance': provenance,
        'fixed_coordinates_um': baseline_coordinates, 'raw_monitor_coordinates': coordinate_evidence,
        'single_variable_groups': {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf8')).hexdigest()
                                   for key, value in group_models.items()},
        'limitations': ['Source-object coordinates are checked in frozen configuration; the FSP object is not reopened.',
                        'HTML audit verifies current-case image membership, not the individual pixels or numerical plotting accuracy.',
                        'Content/provenance consistency does not replace the separate electromagnetic acceptance gates.'],
        'source_files_modified': False}
    audit_path = Path(audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'configs' / 'quick_check.json'))
    parser.add_argument('--require-all', action='store_true', help='Also reject any incomplete queue cases.')
    args = parser.parse_args()
    config, path = load_config(args.config)
    label = 'quick' if 'quick' in path.stem else ('overnight' if 'overnight' in path.stem else re.sub(r'[^A-Za-z0-9_-]', '_', path.stem))
    audit_path = ROOT / 'local_validation' / f'{label}_output_audit.json'
    report = audit_outputs(config, path, audit_path, require_all=args.require_all)
    print('OUTPUT AUDIT:', report['status'], f'{report["valid_done_cases"]}/{report["total_cases"]} valid DONE',
          f'{report["check_count"]} checks', f'{report["error_count"]} errors')
    print('AUDIT JSON:', audit_path)
    for error in report['errors'][:10]:
        print('ERROR:', error)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
