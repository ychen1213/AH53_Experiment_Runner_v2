"""Accept completed named-material planar FDTD cases; never launch a solver."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ah53runner.artifacts import file_sha256, solver_fingerprint
from ah53runner.materials import EFFECTIVE_ABSORPTION_K_THRESHOLD, is_effectively_absorbing
from ah53runner.planning import load_config, plan
from ah53runner.validation import (DEFAULT_TOLERANCES, check_common, check_planar,
                                   load_completed_case)

REPORT_STEM = 'materials_physics_validation'
EVIDENCE_FILES = ('case.json', 'status.json', 'physics_metadata.json',
                  'material_resolution.json', 'result.npz', 'AH53_model.fsp',
                  'worker.log', 'raw_reference_s.npz', 'raw_reference_p.npz',
                  'raw_structure_s.npz', 'raw_structure_p.npz')


def evidence_hashes(folder):
    """Require saved native evidence as well as the validated derived result."""
    hashes = {}
    for name in EVIDENCE_FILES:
        path = Path(folder)/name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f'Missing/empty actual solver evidence: {name}')
        hashes[name] = file_sha256(path)
    return hashes


def _failure(name, detail):
    return {'name': name, 'status': 'FAIL', 'value': None, 'limit': 0., 'detail': detail}


def _output_path(config, config_path):
    output = Path(config['output_dir']).expanduser()
    if not output.is_absolute():
        output = Path(config_path).resolve().parent/output
    return output.resolve()


def evaluate_material_cases(config, config_path):
    """Read current cases and evaluate real coverage; do not write any file."""
    tolerances = dict(DEFAULT_TOLERANCES)
    overrides = config.get('validation_tolerances', {})
    if not isinstance(overrides, dict):
        raise ValueError('validation_tolerances must be an object.')
    for name, value in overrides.items():
        if name not in tolerances:
            raise ValueError(f'Unknown validation tolerance: {name}')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'Validation tolerance must be finite and positive: {name}')
    tolerances.update(overrides)
    path = Path(config_path).resolve()
    output = _output_path(config, path)
    fingerprint = solver_fingerprint()
    cases = plan(config)
    records = []
    coverage = {kind: {'evaluated_cases': [], 'passed_cases': [], 'satisfied': False}
                for kind in ('named_lossless_planar', 'named_absorbing_planar')}
    for case in cases:
        case['solver_fingerprint'] = fingerprint
        model = case['model']
        folder = output/case['id']
        record = {'case_id': case['id'], 'group': case['group'], 'digest': case['digest'],
                  'folder': str(folder), 'checks': [], 'status': 'FAIL'}
        try:
            if model.get('geometry_kind') != 'planar_interface' or not model.get('substrate_material'):
                raise ValueError('This gate requires geometry_kind=planar_interface with a named substrate_material for every planned case.')
            result, metadata = load_completed_case(folder, case)
            hashes = evidence_hashes(folder)
            materials = metadata['materials']
            substrate = materials['roles']['substrate']
            if substrate['kind'] != 'database' or substrate['name'] != model['substrate_material']:
                raise ValueError('Resolved substrate does not match the planned named material.')
            if metadata['solver_fingerprint'] != fingerprint:
                raise ValueError('Recorded solver fingerprint is not the current source fingerprint.')
            version = metadata.get('lumerical_version')
            if not isinstance(version, str) or not version.strip():
                raise ValueError('Actual Lumerical version is absent from solver evidence.')
            classification = ('named_absorbing_planar' if is_effectively_absorbing(substrate)
                              else 'named_lossless_planar')
            checks = check_common(result, tolerances) + check_planar(case, result, tolerances)
            if classification == 'named_absorbing_planar' and substrate['k'] <= 0:
                checks.append(_failure('absorbing_fit_retains_loss',
                                       'A raw absorbing material whose fitted k is nonpositive cannot provide actual absorbing-solver coverage.'))
            # A future change to the shared checks must not silently drop this
            # material-specific physical acceptance requirement.
            names = {item['name'] for item in checks}
            required = {f'fresnel_internal_plane_transmission_{pol}' for pol in ('s', 'p')}
            if not required.issubset(names):
                checks.append(_failure('independent_internal_plane_T_checks',
                                       'Named material acceptance requires independent analytic T checks for both polarizations.'))
            passed = bool(checks) and not any(item['status'] == 'FAIL' for item in checks)
            record.update(status='PASS' if passed else 'FAIL', checks=checks,
                          material_class=classification, lumerical_version=version,
                          solver_fingerprint=metadata['solver_fingerprint'],
                          result_sha256=metadata['result_sha256'], evidence_sha256=hashes,
                          material_resolution_sha256=metadata['material_resolution_sha256'],
                          substrate={key: substrate[key] for key in ('kind', 'name', 'n', 'k', 'raw_n', 'raw_k')},
                          material_definition_sha256=substrate.get('definition_sha256'),
                          material_validation=substrate.get('validation'),
                          material_databases=materials['material_databases'],
                          fit_wavelength_range_nm=materials['fit_wavelength_range_nm'],
                          wavelength_nm=float(result['lambda_nm'][0]), mesh=model['mesh'],
                          power_monitor_planes=metadata['power_monitor_planes'],
                          reference_y_um=model['sim']['reference_y_um'],
                          measured_power={pol: {
                              'R': result['quality'][f'total_reflected_{pol}'],
                              'T_internal_plane': result['quality'][f'total_transmitted_{pol}'],
                              'residual_1_minus_R_minus_T': result['quality'][f'energy_residual_{pol}'],
                          } for pol in ('s', 'p')})
            coverage[classification]['evaluated_cases'].append(case['id'])
            if passed:
                coverage[classification]['passed_cases'].append(case['id'])
        except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError, zipfile.BadZipFile) as error:
            record['checks'] = [_failure('current_named_planar_artifacts', f'{type(error).__name__}: {error}')]
        records.append(record)
    for item in coverage.values():
        item['satisfied'] = bool(item['passed_cases'])
    passed = (bool(records) and all(item['satisfied'] for item in coverage.values())
              and all(record['status'] == 'PASS' for record in records))
    source_hashes = {name: file_sha256(ROOT/name) for name in
                     ('tools/validate_materials.py', 'ah53runner/validation.py',
                      'ah53runner/artifacts.py')}
    return {
        'schema_version': '1.0', 'status': 'PASS' if passed else 'FAIL',
        'evaluated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds'),
        'config': str(path), 'config_file_sha256': file_sha256(path) if path.is_file() else None,
        'planned_config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                                          allow_nan=False).encode('utf8')).hexdigest(),
        'output_dir': str(output), 'solver_fingerprint': fingerprint,
        'validation_source_sha256': source_hashes,
        'scope': 'Named passive isotropic planar substrates at the recorded wavelengths: complex Fresnel reflection, reflected power, internal-plane transmitted power, null-reference leakage and Fourier/raw-flux agreement.',
        'source_policy': 'Reads current DONE artifacts and frozen MDF hashes only; never runs a solver, rewrites results, repairs status, or upgrades phase-calibration metadata.',
        'coverage_policy': 'At least one passing named effectively lossless and one passing named absorbing planar case; every planned case must pass. Raw k>0 always remains absorbing. Only raw-lossless materials with fitted k<=the shared threshold are classified effectively lossless; actual fitted k is retained in both simulation and analytic references. Coverage is never inferred from group names or configured material names.',
        'classification_k_threshold': EFFECTIVE_ABSORPTION_K_THRESHOLD, 'coverage': coverage,
        'total_cases': len(records), 'passed_cases': sum(r['status'] == 'PASS' for r in records),
        'check_count': sum(len(r['checks']) for r in records),
        'failed_check_count': sum(c['status'] == 'FAIL' for r in records for c in r['checks']),
        'tolerances': tolerances, 'tolerance_overrides': overrides,
        'passed_wavelengths_nm': sorted({r['wavelength_nm'] for r in records if r['status'] == 'PASS'}),
        'passed_mdf_cases': [r['case_id'] for r in records if r['status'] == 'PASS' and r['material_databases']],
        'cases': records,
        'limitations': [
            'This gate does not certify arbitrary patterned structures, mesh convergence, or FDTD/RCWA equivalence.',
            'Analytic references use the actual fitted complex index; fit accuracy relative to raw material data is checked separately by the material preflight and recorded here.',
            'T is flux at the configured internal substrate plane. 1-R-T includes absorption and numerical error and is not an independent absorption measurement.',
            'Raw evidence hashes bind the saved inputs to this report; this command does not reopen the FSP or independently rederive every Jones coefficient from raw arrays.',
        ],
    }


def _markdown(report):
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')

    lines = ['# 命名材料 FDTD 平面验收', '', f'**结论：{report["status"]}**', '',
             f'当前求解源码指纹：`{report["solver_fingerprint"]}`。', '',
             f'计划 {report["total_cases"]} 个案例，通过 {report["passed_cases"]} 个；'
             f'{report["check_count"]} 项检查中 {report["failed_check_count"]} 项失败。', '',
             '必须至少通过一个命名近似无损材料和一个命名吸收材料的平面案例，且当前计划的所有案例都通过。原始 k>0 始终按吸收处理；仅原始无损且拟合 k 不超过共享阈值时归入近似无损。实际 k 完整保留，解析透射仍使用该 k。未完成、旧指纹或失败案例不补足通过覆盖。', '',
             f'共享微小拟合损耗阈值：`{report["classification_k_threshold"]:g}`。', '',
             '| 必需覆盖 | 已实际检查 | 通过 | 满足 |', '|---|---:|---:|---|']
    for name, item in report['coverage'].items():
        lines.append(f'| {name} | {len(item["evaluated_cases"])} | {len(item["passed_cases"])} | {item["satisfied"]} |')
    lines.extend(['', '解析检查包含复反射、反射功率及内部透射平面的功率衰减；损耗材料检查 R+T≤1，无损材料保留 R+T≈1 检查。1−R−T 仅为功率余量，不作为独立测量的吸收率。', '',
                  '| Case | 结论 | 材料 | 波长 nm | 拟合 n / k |', '|---|---|---|---:|---|'])
    for record in report['cases']:
        substrate = record.get('substrate', {})
        nk = (f'{substrate["n"]:.9g} / {substrate["k"]:.9g}' if substrate else '不可用')
        lines.append('| '+ ' | '.join(map(cell, (record['case_id'], record['status'],
                     substrate.get('name', '不可用'), record.get('wavelength_nm', '不可用'), nk)))+' |')
    for record in report['cases']:
        lines.extend(['', '## '+cell(record['case_id']), '',
                      '| 检查 | 结论 | 误差/值 | 上限 | 说明 |', '|---|---|---:|---:|---|'])
        for check in record['checks']:
            value = '不可用' if check['value'] is None else f'{check["value"]:.10g}'
            lines.append('| '+ ' | '.join(map(cell, (check['name'], check['status'], value,
                         f'{check["limit"]:.10g}', check['detail'])))+' |')
    lines.extend(['', f'完整阈值、实际版本、监视器位置和文件 SHA256 见 [{REPORT_STEM}.json]({REPORT_STEM}.json)。', '',
                  '本验收仅读取现有求解证据，不修改结果或相位校准状态。通过不代表任意光栅结构已收敛，也不代表 FDTD 与 RCWA 已相互验证。', ''])
    return '\n'.join(lines)


def validate_material_results(config, config_path):
    report = evaluate_material_cases(config, config_path)
    output = Path(report['output_dir'])
    output.mkdir(parents=True, exist_ok=True)
    (output/(REPORT_STEM+'.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                       allow_nan=False), encoding='utf8')
    (output/(REPORT_STEM+'.md')).write_text(_markdown(report), encoding='utf8')
    return report, output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT/'configs/materials_fdtd_validation.json'))
    args = parser.parse_args(argv)
    try:
        config, path = load_config(args.config)
        report, output = validate_material_results(config, path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('MATERIAL PHYSICS VALIDATION REFUSED:', error, file=sys.stderr)
        return 2
    print('MATERIAL PHYSICS VALIDATION:', report['status'],
          f'{report["passed_cases"]}/{report["total_cases"]} cases; {report["check_count"]} checks')
    print('REPORT:', output/(REPORT_STEM+'.md'))
    for record in report['cases']:
        failures = [item['name'] for item in record['checks'] if item['status'] == 'FAIL']
        print(record['status'], record['case_id'], '; '.join(failures))
    for name, coverage in report['coverage'].items():
        if not coverage['satisfied']:
            print('MISSING PASSING COVERAGE:', name)
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
