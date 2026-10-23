"""Check real physics/quick artifacts before an explicitly requested overnight run.

Without --execute this command performs validation only and never starts FDTD.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ah53runner.artifacts import ArtifactError, load_artifacts, solver_fingerprint
from ah53runner.planning import load_config, plan
from ah53runner.validation import DEFAULT_TOLERANCES, check_common, validate_results


def output_path(config, config_path):
    value = Path(config['output_dir']).expanduser()
    return (value if value.is_absolute() else Path(config_path).parent / value).resolve()


def check_cases(config, config_path, tolerances, *, expected_count=None):
    cases = plan(config)
    fingerprint = solver_fingerprint()
    records = []
    errors = []
    if expected_count is not None and len(cases) != expected_count:
        errors.append(f'Expected {expected_count} quick cases, found {len(cases)}.')
    output = output_path(config, config_path)
    for case in cases:
        case['solver_fingerprint'] = fingerprint
        item = {'case_id': case['id'], 'folder': str(output / case['id']), 'status': 'FAIL'}
        try:
            data = load_artifacts(output / case['id'], case)
            result = dict(data['result'], quality=data['quality'])
            item['checks'] = check_common(result, tolerances)
            failures = [check['name'] for check in item['checks'] if check['status'] != 'PASS']
            if failures:
                raise ValueError('Quality tolerance failed: ' + ', '.join(failures))
            item['status'] = 'PASS'
            item['solver_fingerprint'] = data['metadata']['solver_fingerprint']
            item['result_sha256'] = data['metadata']['result_sha256']
        except (ArtifactError, OSError, ValueError, KeyError, TypeError) as error:
            item['error'] = str(error)
            errors.append(f'{case["id"]}: {error}')
        records.append(item)
    return {'status': 'FAIL' if errors else 'PASS', 'output': str(output),
            'case_count': len(cases), 'cases': records, 'errors': errors}


def evaluate_gate(physics_config_path, quick_config_path):
    fingerprint = solver_fingerprint()
    report = {'schema_version': '1.0',
              'evaluated_at': datetime.now().astimezone().isoformat(),
              'solver_fingerprint': fingerprint, 'status': 'FAIL', 'errors': [],
              'simulation_started_during_gate': False}
    physics_config, physics_path = load_config(physics_config_path)
    quick_config, quick_path = load_config(quick_config_path)
    tolerances = dict(DEFAULT_TOLERANCES)
    tolerances.update(physics_config.get('validation_tolerances', {}))
    # Honor either configured gate without allowing warning limits to loosen the benchmark.
    for metric, configured in (('power_closure_absolute', 'closure_warn_above'),
                               ('flux_decomposition_absolute', 'flux_warn_above')):
        value = float(quick_config.get(configured, tolerances[metric]))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{configured} must be finite and positive.')
        tolerances[metric] = min(float(tolerances[metric]), value)
    report['quality_tolerances'] = tolerances

    # Re-evaluate current real files; an old PASS file alone never opens the gate.
    physics, physics_output = validate_results(physics_config, physics_path)
    report['physics'] = {'report': str(physics_output / 'physics_validation.json'),
                         'status': physics.get('status'), 'coverage': physics.get('coverage'),
                         'solver_fingerprint': physics.get('solver_fingerprint')}
    required_coverage = ('air_interface', 'n15_interface', 'symmetric_ah53', 'rigid_translation')
    if physics.get('status') != 'PASS':
        report['errors'].append('Current real physics benchmarks have not passed; inspect physics_validation.html.')
    if physics.get('solver_fingerprint') != fingerprint:
        report['errors'].append('Physics report solver fingerprint is absent or differs from the current solver.')
    if not all(physics.get('coverage', {}).get(name) is True for name in required_coverage):
        report['errors'].append('Physics coverage requires air, n=1.5, symmetry and rigid translation.')
    report['quick'] = check_cases(quick_config, quick_path, tolerances, expected_count=3)
    report['errors'].extend(report['quick']['errors'])
    if solver_fingerprint() != fingerprint:
        report['errors'].append('Solver source changed during validation; repeat the gate.')
    report['status'] = 'FAIL' if report['errors'] else 'PASS'
    return report, tolerances


def run_overnight(config_path, gate_fingerprint):
    config, path = load_config(config_path)
    cases = plan(config)
    output = output_path(config, path)
    retry_needed = False
    pending_needed = False
    retry_states = {'FAILED', 'RUNNING', 'DONE', 'INCOMPLETE', 'INTERRUPTED'}
    for case in cases:
        case['solver_fingerprint'] = gate_fingerprint
        folder = output / case['id']
        try:
            load_artifacts(folder, case)
            continue
        except ArtifactError:
            pass
        try:
            state = json.loads((folder / 'status.json').read_text(encoding='utf-8')).get('status', 'PENDING')
        except (OSError, ValueError, AttributeError):
            state = 'PENDING'
        if state in retry_states:
            retry_needed = True
        else:
            pending_needed = True
    base = [sys.executable, str(ROOT / 'run_experiments.py')]
    commands = []
    # Retry old failures before new pending work, so fresh failures are not retried this same night.
    if retry_needed:
        commands.append(base + ['run', '--config', str(path), '--retry-failed'])
    if pending_needed:
        commands.append(base + ['run', '--config', str(path)])
    codes = []
    for command in commands:
        if solver_fingerprint() != gate_fingerprint:
            print('BLOCKED: solver changed after gate; no further runs launched.', flush=True)
            return 2
        print('COMMAND:', json.dumps(command, ensure_ascii=False), flush=True)
        code = subprocess.run(command, cwd=ROOT).returncode
        codes.append(code)
        # rc=1 reports ordinary failed cases after the queue has continued.
        # Pending work must still run after an old failed case fails again.
        # Interrupts and abnormal exits stop dispatching further commands.
        if code not in (0, 1):
            break
    report_command = base + ['report', '--config', str(path)]
    print('COMMAND:', json.dumps(report_command, ensure_ascii=False), flush=True)
    codes.append(subprocess.run(report_command, cwd=ROOT).returncode)
    if any(codes):
        return next(code for code in codes if code)
    incomplete = []
    for case in cases:
        try:
            load_artifacts(output / case['id'], case)
        except ArtifactError as error:
            incomplete.append(f'{case["id"]}: {error}')
    for error in incomplete:
        print('INCOMPLETE:', error, flush=True)
    return 3 if incomplete else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--physics-config', default=str(ROOT / 'configs' / 'physics_validation.json'))
    parser.add_argument('--quick-config', default=str(ROOT / 'configs' / 'quick_check.json'))
    parser.add_argument('--config', default=str(ROOT / 'configs' / 'overnight_example.json'))
    parser.add_argument('--gate-report', default=str(ROOT / 'local_validation' / 'overnight_gate.json'))
    parser.add_argument('--execute', action='store_true', help='Launch the overnight queue only after every gate passes.')
    args = parser.parse_args()
    try:
        report, unused_tolerances = evaluate_gate(args.physics_config, args.quick_config)
    except (OSError, ValueError, KeyError, TypeError) as error:
        report = {'schema_version': '1.0', 'status': 'FAIL',
                  'evaluated_at': datetime.now().astimezone().isoformat(),
                  'solver_fingerprint': solver_fingerprint(),
                  'simulation_started_during_gate': False, 'errors': [str(error)]}
    report['execution_requested'] = args.execute
    destination = Path(args.gate_report).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('OVERNIGHT GATE:', report['status'], flush=True)
    print('GATE REPORT:', destination, flush=True)
    for error in report['errors']:
        print('BLOCKED:', error, flush=True)
    if report['status'] != 'PASS':
        print('No overnight simulation was started.', flush=True)
        return 2
    if not args.execute:
        print('Validation only. Use --execute to start the validated overnight queue.', flush=True)
        return 0
    report['queue_started_at'] = datetime.now().astimezone().isoformat()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    code = run_overnight(args.config, report['solver_fingerprint'])
    report['queue_finished_at'] = datetime.now().astimezone().isoformat()
    report['queue_exit_code'] = code
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
