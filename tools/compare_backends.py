"""Compare real, calibrated RCWA reflection with matching real FDTD artifacts.

The command does not start solvers or modify input artifacts. Only its own
CSV/JSON/HTML diagnostic files are written. Transmission is not compared.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import html
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ah53runner import rcwa
from ah53runner.artifacts import load_artifacts, solver_fingerprint, strict_json_loads
from ah53runner.geometry import build_geometry, shapes_to_json

# Fixed before acquiring cross-backend comparison data. Do not relax these
# thresholds to fit a computed result; refine numerical settings instead.
TOLERANCES = {'complex_relative_l2': 0.03, 'phase_deg': 2.,
              'absolute_amplitude_floor': 1e-4, 'relative_amplitude_floor': 1e-3,
              'total_reflected_power_absolute': 0.01}
COMPONENTS = {'rss': (0, 0), 'rsp': (0, 1), 'rps': (1, 0), 'rpp': (1, 1)}
CSV_COLUMNS = ['m', 'component', 'fdtd_real', 'fdtd_imag', 'rcwa_real', 'rcwa_imag',
               'fdtd_abs', 'rcwa_abs', 'complex_absolute_error', 'fdtd_power_real',
               'fdtd_power_imag', 'rcwa_power_real', 'rcwa_power_imag',
               'phase_floor', 'phase_difference_deg', 'phase_status']


def require(condition, message):
    if not condition:
        raise ValueError(message)


def matching_geometry(fdtd_model, rcwa_model):
    for key in ('period_nm', 'wavelength_nm'):
        require(fdtd_model[key] == rcwa_model[key], f'Physical {key} differs between backends.')
    fdtd_shapes, fdtd_top, _ = build_geometry(fdtd_model)
    rcwa_shapes, rcwa_top, _ = build_geometry(rcwa_model)
    require(shapes_to_json(fdtd_shapes) == shapes_to_json(rcwa_shapes) and fdtd_top == rcwa_top,
            'Physical geometry/materials differ; only numerical mesh settings may differ.')
    for key in ('reference_y_um', 'transmission_y_um'):
        require(fdtd_model['sim'][key] == rcwa_model['sim'][key], f'Physical reference plane {key} differs.')
    return {'shapes': shapes_to_json(fdtd_shapes), 'top_nm': fdtd_top,
            'period_nm': fdtd_model['period_nm'], 'wavelength_nm': fdtd_model['wavelength_nm'],
            'reflection_reference_y_um': fdtd_model['sim']['reference_y_um']}


def load_inputs(fdtd_folder, rcwa_folder, certificate_path):
    fdtd_case = strict_json_loads((fdtd_folder / 'case.json').read_text(encoding='utf8'))
    fdtd_case['solver_fingerprint'] = solver_fingerprint()
    fdtd = load_artifacts(fdtd_folder, fdtd_case)
    require(fdtd['metadata'].get('physical_model') == '2D lossless dielectric XY', 'Unsupported FDTD physical model.')
    require(fdtd['quality']['incident_spatial_phase_sign'] == -1, 'Unsupported FDTD time/spatial phase convention.')
    require(fdtd['metadata'].get('basis') == {'input_s': 'Ez', 'input_p': '+Ex',
            'output_s': 'Ez', 'output_p': '(cos(theta),-sin(theta),0)'}, 'FDTD polarization basis is not the shared basis.')
    frozen, state, native = rcwa.read_acquisition(rcwa_folder)
    certificate = strict_json_loads(certificate_path.read_text(encoding='utf8'))
    rcwa.require_phase_calibration(certificate, frozen['case']['model'], frozen['settings'], state['lumerical_version'])
    metadata = strict_json_loads((rcwa_folder / 'rcwa_coefficients.metadata.json').read_text(encoding='utf8'))
    require(metadata.get('phase_status') == 'CALIBRATED_ON_CURRENT_RCWA_CONTROL_FAMILY',
            'Native or uncalibrated RCWA data cannot be evaluated as a calibrated comparison.')
    require(metadata.get('backend_fingerprint') == rcwa.backend_fingerprint(), 'RCWA coefficient backend fingerprint is stale.')
    require(metadata.get('input_sha256') == state['input_sha256'], 'RCWA export input hash differs from the actual native acquisition.')
    require(metadata.get('lumerical_version') == state['lumerical_version'], 'RCWA export/native solver versions differ.')
    require(metadata.get('scope') == certificate['scope'] and metadata.get('registration') == certificate['registration'],
            'RCWA export scope or registration differs from the validated certificate.')
    require(metadata.get('certificate_sha256') == rcwa.canonical_hash(certificate), 'RCWA export certificate hash differs.')
    coefficients = rcwa_folder / 'rcwa_coefficients.npz'
    require(metadata.get('result_sha256') == rcwa.sha256(coefficients), 'RCWA coefficient checksum mismatch.')
    with np.load(coefficients, allow_pickle=False) as archive:
        converted = {key: archive[key].copy() for key in archive.files}
    recomputed, diagnostics = rcwa.convert_native(native, frozen['case']['model'], frozen['settings'], certificate['registration'])
    for key in ('r_orders', 'r_theta_deg', 'r_E', 'r_power', 'S11', 'eta_r_s', 'eta_r_p', 'lambda_nm'):
        require(key in converted and converted[key].shape == recomputed[key].shape
                and np.isfinite(converted[key]).all()
                and np.allclose(converted[key], recomputed[key], rtol=1e-12, atol=1e-12),
                f'RCWA exported {key} does not reproduce the calibrated native conversion.')
    geometry = matching_geometry(fdtd_case['model'], frozen['case']['model'])
    require(frozen['geometry'] == rcwa.geometry_spec(frozen['case']['model']), 'RCWA frozen geometry is not the shared shape geometry.')
    require(metadata.get('materials') == frozen['geometry']['source_shapes_xy_nm'], 'RCWA exported materials differ from frozen geometry.')
    require(fdtd['metadata'].get('reference_y_um') == geometry['reflection_reference_y_um'], 'FDTD metadata reference plane mismatch.')
    require(metadata.get('reference_planes', {}).get('reflection_reference_z_nm') == -1000 * geometry['reflection_reference_y_um'],
            'RCWA reflection reference plane does not rotate to the FDTD reference plane.')
    require(float(fdtd['result']['lambda_nm'][0]) == float(converted['lambda_nm'][0]), 'Actual selected wavelengths differ.')
    return fdtd, converted, {'geometry': geometry,
        'fdtd': {'folder': str(fdtd_folder), 'case_id': fdtd_case['id'], 'digest': fdtd_case['digest'],
                 'solver_fingerprint': fdtd['metadata']['solver_fingerprint'],
                 'result_sha256': fdtd['metadata']['result_sha256'],
                 'lumerical_version': fdtd['metadata']['lumerical_version'],
                 'recorded_phase_status': fdtd['metadata']['jones_phase_status']},
        'rcwa': {'folder': str(rcwa_folder), 'backend_fingerprint': metadata['backend_fingerprint'],
                 'input_sha256': state['input_sha256'], 'result_sha256': metadata['result_sha256'],
                 'lumerical_version': state['lumerical_version'], 'certificate_sha256': metadata['certificate_sha256'],
                 'registration': metadata['registration'], 'phase_status': metadata['phase_status']}}


def compare_reflection(fdtd, converted):
    """Numerical diagnosis only after provenance/calibration checks have passed."""
    f, q = fdtd['result'], fdtd['quality']
    aorders, borders = np.asarray(f['orders']), np.asarray(converted['r_orders'])
    common = np.intersect1d(aorders, borders)
    require(np.array_equal(aorders, common) and np.array_equal(borders, common),
            'Complete propagating reflection order sets must agree; a partial intersection cannot PASS.')
    require(len(common) and 0 in common, 'No common propagating reflection orders.')
    checks, rows = [], []
    def check(name, value, limit, detail=''):
        checks.append({'name': name, 'value': float(value), 'limit': float(limit),
                       'status': 'PASS' if math.isfinite(float(value)) and value <= limit else 'FAIL', 'detail': detail})
    selected = np.abs(common) <= 5
    for label, left, right in (('r_E', f['r_E'], converted['r_E']),
                               ('r_power', f['r_power'], converted['r_power'])):
        norm = float(np.linalg.norm(right[selected]))
        require(norm > 1e-12, f'{label}: reflection norm is unresolved for a relative comparison.')
        error = float(np.linalg.norm(left[selected] - right[selected]) / norm)
        check(label + '_relative_L2_abs_m_le_5', error, TOLERANCES['complex_relative_l2'])
    phase_orders = {0, -1, 1, -5, 5}
    for name, (out, incoming) in COMPONENTS.items():
        component_max = max(float(np.max(np.abs(f['r_E'][:, out, incoming]))),
                            float(np.max(np.abs(converted['r_E'][:, out, incoming]))))
        floor = max(TOLERANCES['absolute_amplitude_floor'], TOLERANCES['relative_amplitude_floor'] * component_max)
        for i, order in enumerate(common):
            a, b = complex(f['r_E'][i, out, incoming]), complex(converted['r_E'][i, out, incoming])
            ap, bp = complex(f['r_power'][i, out, incoming]), complex(converted['r_power'][i, out, incoming])
            both_strong = min(abs(a), abs(b)) >= floor
            one_strong = max(abs(a), abs(b)) >= floor
            phase = math.degrees(math.atan2((a*b.conjugate()).imag, (a*b.conjugate()).real)) if both_strong else None
            if int(order) in phase_orders and both_strong:
                check(f'phase_{name}_m{int(order)}', abs(phase), TOLERANCES['phase_deg'])
            elif int(order) in phase_orders and one_strong:
                check(f'phase_observability_{name}_m{int(order)}', 1, 0,
                      'Only one backend resolves this channel above the predeclared amplitude floor.')
            rows.append({'m': int(order), 'component': name,
                'fdtd_real': a.real, 'fdtd_imag': a.imag, 'rcwa_real': b.real, 'rcwa_imag': b.imag,
                'fdtd_abs': abs(a), 'rcwa_abs': abs(b), 'complex_absolute_error': abs(a-b),
                'fdtd_power_real': ap.real, 'fdtd_power_imag': ap.imag,
                'rcwa_power_real': bp.real, 'rcwa_power_imag': bp.imag,
                'phase_floor': floor, 'phase_difference_deg': phase,
                'phase_status': ('CHECKED' if int(order) in phase_orders else 'DIAGNOSTIC') if both_strong
                                else ('UNRESOLVED_ONE_BACKEND' if one_strong else 'SUPPRESSED_LOW_AMPLITUDE')})
    totals = {}
    for j, pol in enumerate(('s', 'p')):
        forders = float(np.sum(np.abs(f['r_power'][:, :, j])**2))
        rorders = float(np.sum(np.abs(converted['r_power'][:, :, j])**2))
        fraw = float(q['total_reflected_' + pol])
        totals[pol] = {'fdtd_raw_monitor': fraw, 'fdtd_sum_orders': forders, 'rcwa_sum_orders': rorders}
        check('total_reflected_raw_power_' + pol, abs(fraw-rorders), TOLERANCES['total_reflected_power_absolute'])
        check('total_reflected_order_power_' + pol, abs(forders-rorders), TOLERANCES['total_reflected_power_absolute'])
    return {'status': 'PASS' if checks and all(c['status'] == 'PASS' for c in checks) else 'FAIL',
            'evaluated': True, 'checks': checks, 'common_orders': common.tolist(),
            'complete_order_coverage': True, 'total_reflected_power': totals}, rows


def compare(fdtd_folder, rcwa_folder, certificate_path, output_dir):
    fdtd_folder, rcwa_folder, certificate_path = (Path(p).resolve() for p in (fdtd_folder, rcwa_folder, certificate_path))
    output_dir = Path(output_dir).resolve()
    require(not output_dir.is_relative_to(fdtd_folder) and not output_dir.is_relative_to(rcwa_folder),
            'Comparison output directory must be separate from both input case directories.')
    rows = []
    try:
        fdtd, converted, provenance = load_inputs(fdtd_folder, rcwa_folder, certificate_path)
        report, rows = compare_reflection(fdtd, converted)
        report['provenance'] = provenance
    except Exception as error:
        report = {'status': 'REFUSED', 'evaluated': False, 'checks': [],
                  'errors': [f'{type(error).__name__}: {error}']}
    report.update(schema_version='1.0',
        evaluated_at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds'),
        scope='Reflection only, top zero-order s/p incident columns, shared air-port basis and fixed reference plane.',
        tolerances=TOLERANCES,
        transmission_validation='NOT PERFORMED: FDTD has not published t_E; RCWA transmission is not cross-backend validated.',
        solver_runs_performed=False, input_files_modified=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / 'reflection_comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    (output_dir / 'backend_comparison.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
    body = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>FDTD/RCWA反射对照</title>',
            '<style>body{font:15px/1.65 system-ui;max-width:1100px;margin:30px auto;padding:0 20px}table{border-collapse:collapse;width:100%}th,td{padding:6px;border-bottom:1px solid #ddd;text-align:left}.FAIL,.REFUSED{color:#b00}</style>',
            f'<h1>FDTD / RCWA 反射对照：{report["status"]}</h1>',
            '<p>仅在真实 FDTD 文件和可重新验证的 RCWA 校准证书均完整时评估。同一物理截面、材料、波长、基矢和参考面必须一致，数值网格可不同。门限预先固定，失败后应检查收敛。</p>',
            '<p><b>透射未作跨后端校验：</b>FDTD 当前未发布 t_E。此页不声称完整双侧 S 矩阵已验证。</p>',
            '<p><a href="backend_comparison.json">完整来源与门限</a>　<a href="reflection_comparison.csv">完整各阶复系数对照</a></p>']
    for error in report.get('errors', []):
        body.append('<p class="REFUSED">' + html.escape(error) + '</p>')
    if report.get('evaluated'):
        body.append(f'<p>完整共同反射阶数：{len(report["common_orders"])}。弱振幅相位标记无效。</p><table><tr><th>检查</th><th>值/误差</th><th>门限</th><th>结论</th></tr>')
        for check in report['checks']:
            body.append('<tr class="' + check['status'] + '"><td>' + html.escape(check['name']) + '</td><td>' + f'{check["value"]:.9g}' + '</td><td>' + f'{check["limit"]:.9g}' + '</td><td>' + check['status'] + '</td></tr>')
        body.append('</table>')
    body.append('</html>')
    (output_dir / 'backend_comparison.html').write_text('\n'.join(body), encoding='utf8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fdtd-case', required=True)
    parser.add_argument('--rcwa-case', required=True)
    parser.add_argument('--certificate', default=str(ROOT / 'output_ah53_rcwa_validation' / 'rcwa_phase_calibration.json'))
    parser.add_argument('--output-dir', default=str(ROOT / 'local_validation' / 'backend_comparison'))
    args = parser.parse_args()
    report = compare(args.fdtd_case, args.rcwa_case, args.certificate, args.output_dir)
    print('BACKEND COMPARISON:', report['status'], 'evaluated=', report['evaluated'])
    print('REPORT:', Path(args.output_dir).resolve() / 'backend_comparison.html')
    for error in report.get('errors', []):
        print('REFUSED:', error)
    return 0 if report['status'] == 'PASS' else (2 if report['status'] == 'REFUSED' else 1)


if __name__ == '__main__':
    raise SystemExit(main())
