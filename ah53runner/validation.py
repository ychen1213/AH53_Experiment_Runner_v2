"""Minimum electromagnetic benchmarks, evaluated only from completed FDTD cases.

The analytic functions are independently unit-testable. This module never runs a
solver, writes a result.npz, or upgrades solver metadata to calibrated status.
"""
from __future__ import annotations

import datetime
import html
import json
import math
from pathlib import Path
import zipfile

import numpy as np

from .planning import plan
from .artifacts import load_artifacts, solver_fingerprint


DEFAULT_TOLERANCES = {
    'phase_amplitude_floor': 1e-4,
    'complex_absolute': 0.01,
    'phase_deg': 2.0,
    'power_closure_absolute': 0.03,
    'flux_decomposition_absolute': 0.01,
    # These accuracy limits are fixed before the next physical data generation;
    # failed solver outputs must not be made to pass by relaxing the limits.
    'reference_leakage_power_max': 1e-6,
    'flux_decomposition_relative': 0.05,
    'flux_reflection_floor': 1e-3,
    'air_reflected_power_max': 1e-4,
    'fresnel_power_absolute': 0.005,
    'parasitic_orders_power_max': 1e-4,
    'symmetry_power_absolute': 1e-5,
    'symmetry_power_relative': 0.02,
    'translation_amplitude_relative': 0.03,
}


def fresnel_at_reference(n_substrate, wavelength_nm, reference_y_um,
                         incident_spatial_phase_sign, interface_y_nm=0.0):
    """Normal incidence, air input, s=+Z and both normal p vectors=+X.

    If incident E varies as exp(i*sign*k*y), the ratio at y_ref is
    ((1-n)/(1+n))*exp(-2*i*sign*k*(y_ref-y_interface)). Both diagonal
    Jones entries therefore equal -0.2 at the n=1.5 interface itself.
    """
    if incident_spatial_phase_sign not in (-1, 1):
        raise ValueError('Incident spatial phase sign must be -1 or +1.')
    n_substrate = complex(n_substrate)
    if (not np.isfinite(n_substrate) or n_substrate.real < -1e-12
            or n_substrate.imag < -1e-12 or abs(n_substrate) == 0 or wavelength_nm <= 0):
        raise ValueError('Require a finite passive index and positive wavelength.')
    phase = (-2j * incident_spatial_phase_sign * 2 * math.pi
             * (reference_y_um * 1000 - interface_y_nm) / wavelength_nm)
    return (1 - n_substrate) / (1 + n_substrate) * np.exp(phase)


def translation_factor(order, shift_nm, period_nm):
    """Fourier convention integral(E*exp(-i*2*pi*m*x/P))/P."""
    return np.exp(-2j * math.pi * order * shift_nm / period_nm)


def wrapped_phase_error_deg(measured, expected):
    return float(abs(np.rad2deg(np.angle(measured * np.conj(expected)))))


def _check(name, value, limit, *, detail='', suppressed=False):
    numeric = float(value) if value is not None else None
    return {'name': name,
            'status': 'SUPPRESSED' if suppressed else (
                'PASS' if numeric is not None and math.isfinite(numeric)
                and numeric <= limit else 'FAIL'),
            'value': numeric, 'limit': float(limit), 'detail': detail}


def check_common(result, tolerances):
    q = result['quality']
    checks = []
    checks.append(_check('isotropic_2d_cross_polarization',
                         max(np.max(np.abs(result['r_E'][:, 0, 1])),
                             np.max(np.abs(result['r_E'][:, 1, 0]))), 1e-8))
    for pol in ('s', 'p'):
        reference_power = q[f'air_reference_reflected_{pol}']
        if (not isinstance(reference_power, (int, float))
                or not math.isfinite(reference_power) or reference_power < 0):
            raise ValueError(f'Invalid measured air-reference reflected power: {pol}')
        checks.append(_check(f'air_reference_leakage_{pol}', reference_power,
                             tolerances['reference_leakage_power_max'],
                             detail='Required independently measured null reflection; no inferred or filled value.'))
        if q.get('absorption_present', False):
            checks.append(_check(f'passive_flux_bound_{pol}',
                                 max(0., q[f'power_closure_{pol}'] - 1),
                                 tolerances['power_closure_absolute'],
                                 detail='R+T_plane may be below 1 due to absorption. This bound does not independently validate absorption or numerical convergence.'))
        else:
            checks.append(_check(f'energy_closure_{pol}',
                                 abs(q[f'power_closure_{pol}'] - 1),
                                 tolerances['power_closure_absolute']))
        if f'signed_reflection_flux_{pol}' in q:
            checks.append(_check(f'outgoing_flux_direction_{pol}',
                                 max(0., -q[f'signed_reflection_flux_{pol}'],
                                     q[f'signed_transmission_flux_{pol}']),
                                 tolerances['power_closure_absolute'],
                                 detail='Reflection is +Y and transmission is -Y; signed native flux retained.'))
        checks.append(_check(f'fourier_flux_agreement_{pol}',
                             q[f'flux_decomposition_error_{pol}'],
                             tolerances['flux_decomposition_absolute']))
        relative_flux_error = (q[f'flux_decomposition_error_{pol}']
                               / max(q[f'total_reflected_{pol}'],
                                     tolerances['flux_reflection_floor']))
        checks.append(_check(f'fourier_flux_relative_{pol}', relative_flux_error,
                             tolerances['flux_decomposition_relative'],
                             detail='absolute discrepancy / max(raw reflected power, configured floor)'))
    ct = np.cos(np.deg2rad(result['theta_deg']))
    expected_power = result['r_E'] * np.sqrt(ct)[:, None, None]
    checks.append(_check('power_amplitude_definition',
                         np.max(np.abs(result['r_power'] - expected_power)), 1e-10))
    for j, pol in enumerate(('s', 'p')):
        eta = ct * np.sum(np.abs(result['r_E'][:, :, j]) ** 2, axis=1)
        checks.append(_check(f'order_power_definition_{pol}',
                             np.max(np.abs(result[f'eta_{pol}'] - eta)), 1e-10))
    return checks


def check_planar(case, result, tolerances):
    cfg = case['model']
    q = result['quality']
    if cfg.get('substrate_material'):
        substrate = result['materials']['roles']['substrate']
        n = complex(substrate['n'], substrate['k'])
    else:
        n = complex(cfg['n_substrate'])
    expected = fresnel_at_reference(n, float(result['lambda_nm'][0]),
                                    cfg['sim']['reference_y_um'],
                                    q['incident_spatial_phase_sign'])
    i0 = int(np.flatnonzero(result['orders'] == 0)[0])
    checks = []
    for j, pol in enumerate(('s', 'p')):
        measured = result['r_E'][i0, j, j]
        checks.append(_check(f'fresnel_complex_{pol}', abs(measured - expected),
                             tolerances['complex_absolute'],
                             detail=f'expected=({expected.real:.12g},{expected.imag:.12g}) at fixed y_ref'))
        if abs(expected) > tolerances['phase_amplitude_floor']:
            suppressed = abs(measured) < tolerances['phase_amplitude_floor']
            checks.append(_check(f'fresnel_phase_{pol}',
                                 None if suppressed else wrapped_phase_error_deg(measured, expected),
                                 tolerances['phase_deg'], suppressed=suppressed,
                                 detail='Phase undefined below amplitude floor.' if suppressed else ''))
            if suppressed:
                checks.append(_check(f'fresnel_phase_observable_{pol}', 1, 0,
                                     detail='Nonzero analytic Fresnel amplitude was not resolved.'))
        else:
            checks.append(_check(f'air_phase_{pol}', None, tolerances['phase_deg'],
                                 suppressed=True, detail='Analytic reflection is zero; phase has no meaning.'))
        expected_R = abs((1 - n) / (1 + n)) ** 2
        limit = (tolerances['air_reflected_power_max'] if n == 1
                 else tolerances['fresnel_power_absolute'])
        checks.append(_check(f'fresnel_total_power_{pol}',
                             abs(q[f'total_reflected_{pol}'] - expected_R), limit))
        # The original constant-n benchmark retains its established checks.
        # Named substrates additionally validate material-dependent attenuation.
        if cfg.get('substrate_material') and 'materials' in result and f'total_transmitted_{pol}' in q:
            depth_nm = -1000*float(cfg['sim']['transmission_y_um'])
            if depth_nm <= 0:
                raise ValueError('Planar transmission validation requires a plane inside the substrate.')
            expected_T = (n.real * abs(2/(1+n))**2
                          * np.exp(-4*np.pi*n.imag*depth_nm/float(result['lambda_nm'][0])))
            checks.append(_check(f'fresnel_internal_plane_transmission_{pol}',
                                 abs(q[f'total_transmitted_{pol}']-expected_T),
                                 tolerances['fresnel_power_absolute'],
                                 detail=f'Independent half-space Fresnel flux with exp(-4*pi*k*depth/lambda); depth={depth_nm:g} nm, expected T={expected_T:.12g}.'))
        nonzero = result['orders'] != 0
        checks.append(_check(f'planar_parasitic_orders_{pol}',
                             np.sum(result[f'eta_{pol}'][nonzero]),
                             tolerances['parasitic_orders_power_max']))
    return checks


def check_symmetry(result, tolerances):
    checks = []
    orders = result['orders']
    for j, pol in enumerate(('s', 'p')):
        observable = 0
        for m in orders[orders > 0]:
            ip = int(np.flatnonzero(orders == m)[0])
            im = int(np.flatnonzero(orders == -m)[0])
            ep, em = result[f'eta_{pol}'][[ip, im]]
            limit = max(tolerances['symmetry_power_absolute'],
                        tolerances['symmetry_power_relative'] * max(ep, em))
            checks.append(_check(f'symmetry_power_{pol}_m{m}', abs(ep - em), limit))
            ap, am = result['r_E'][[ip, im], j, j]
            weak = min(abs(ap), abs(am)) < tolerances['phase_amplitude_floor']
            if not weak:
                observable += 1
            checks.append(_check(f'symmetry_phase_{pol}_m{m}',
                                 None if weak else wrapped_phase_error_deg(ap, am),
                                 tolerances['phase_deg'], suppressed=weak,
                                 detail='Below amplitude floor.' if weak else ''))
        checks.append(_check(f'symmetry_phase_observable_{pol}', 0 if observable else 1, 0,
                             detail=f'{observable} nonzero order pairs resolved.'))
    return checks


def check_translation(reference_case, reference, moved_case, moved, tolerances):
    dx = float(moved_case['model']['ridge_shift_nm']) - float(reference_case['model']['ridge_shift_nm'])
    period = float(reference_case['model']['period_nm'])
    checks = []
    checks.append(_check('translation_wavelength_match_nm',
                         abs(float(reference['lambda_nm'][0]) - float(moved['lambda_nm'][0])), 1e-6))
    checks.append(_check('translation_phase_convention_match',
                         int(reference['quality']['incident_spatial_phase_sign'] != moved['quality']['incident_spatial_phase_sign']), 0))
    for j, pol in enumerate(('s', 'p')):
        for m in (1, 5):
            coefficients = []
            for result, order in ((reference, m), (reference, -m), (moved, m), (moved, -m)):
                pos = np.flatnonzero(result['orders'] == order)
                coefficients.append(result['r_E'][int(pos[0]), j, j] if len(pos) else 0j)
            a0, b0, a1, b1 = coefficients
            weak = min(map(abs, coefficients)) < tolerances['phase_amplitude_floor']
            checks.append(_check(f'translation_observable_{pol}_m{m}', int(weak), 0,
                                 detail='Both +/- orders must exceed phase amplitude floor.'))
            if weak:
                checks.append(_check(f'translation_pair_phase_{pol}_m{m}', None,
                                     tolerances['phase_deg'], suppressed=True,
                                     detail='Below amplitude floor; phase is not interpreted.'))
                continue
            for order, z0, z1 in ((m, a0, a1), (-m, b0, b1)):
                factor = translation_factor(order, dx, period)
                checks.append(_check(f'translation_phase_{pol}_m{order}',
                                     wrapped_phase_error_deg(z1 / z0, factor),
                                     tolerances['phase_deg'],
                                     detail=f'Expected signed change {np.rad2deg(np.angle(factor)):.9g} deg.'))
                checks.append(_check(f'translation_magnitude_{pol}_m{order}',
                                     abs(abs(z1) / abs(z0) - 1),
                                     tolerances['translation_amplitude_relative']))
            pair_ratio = a1 * np.conj(b1) / (a0 * np.conj(b0))
            expected_pair = translation_factor(2*m, dx, period)
            checks.append(_check(f'translation_pair_phase_{pol}_m{m}',
                                 wrapped_phase_error_deg(pair_ratio, expected_pair),
                                 tolerances['phase_deg'],
                                 detail=f'Expected +/- relative change {np.rad2deg(np.angle(expected_pair)):.9g} deg.'))
    return checks


def load_completed_case(folder, case):
    """Use the shared provenance/checksum/shape validator, then check case identity."""
    folder = Path(folder)
    for name in ('case.json', 'status.json', 'physics_metadata.json', 'result.npz', 'AH53_model.fsp', 'worker.log'):
        if not (folder / name).is_file():
            raise ValueError(f'Missing required artifact: {name}')
    frozen = json.loads((folder / 'case.json').read_text(encoding='utf8'))
    if frozen.get('digest') != case['digest'] or frozen.get('model') != case['model']:
        raise ValueError('Frozen case differs from the current benchmark configuration.')
    artifacts = load_artifacts(folder, case)
    metadata = artifacts['metadata']
    if metadata.get('physical_model') not in (
            '2D lossless dielectric XY', '2D passive isotropic dispersive XY',
            '2D passive isotropic dispersive absorbing XY'):
        raise ValueError('Physical model is not the supported 2D passive isotropic FDTD model.')
    result = artifacts['result']
    result['quality'] = artifacts['quality']
    if 'materials' in metadata:
        result['materials'] = metadata['materials']
    return result, metadata


def validate_results(config, config_path):
    """Write a separate benchmark verdict. Missing runs are explicit failures."""
    tolerances = dict(DEFAULT_TOLERANCES)
    tolerances.update(config.get('validation_tolerances', {}))
    for key, value in tolerances.items():
        if not math.isfinite(float(value)) or value <= 0:
            raise ValueError(f'Validation tolerance must be finite and positive: {key}')
    output = Path(config['output_dir']).expanduser()
    if not output.is_absolute():
        output = Path(config_path).resolve().parent / output
    output = output.resolve()
    cases = plan(config)
    fingerprint = solver_fingerprint()
    for case in cases:
        case['solver_fingerprint'] = fingerprint
    records = []
    loaded = {}
    for case in cases:
        record = {'case_id': case['id'], 'group': case['group'],
                  'folder': str(output / case['id']), 'checks': []}
        try:
            result, metadata = load_completed_case(output / case['id'], case)
            record['recorded_phase_status'] = metadata.get('jones_phase_status')
            record['digest'] = case['digest']
            record['solver_fingerprint'] = metadata['solver_fingerprint']
            record['result_sha256'] = metadata['result_sha256']
            record['checks'] = check_common(result, tolerances)
            if case['model'].get('geometry_kind') == 'planar_interface':
                record['checks'].extend(check_planar(case, result, tolerances))
            elif float(case['model'].get('ridge_shift_nm', 0)) == 0:
                model = case['model']
                symmetric = (model.get('ridge_center_offsets_nm', [0, 0, 0]) == [0, 0, 0]
                             and model.get('ridge_top_left_inset_nm', 0) == model.get('ridge_top_right_inset_nm', 0))
                if symmetric:
                    record['checks'].extend(check_symmetry(result, tolerances))
                else:
                    record['checks'].append(_check('symmetric_geometry_input', 1, 0,
                                                   detail='Zero shift alone does not imply mirror symmetry.'))
            loaded[case['id']] = result
        except (OSError, ValueError, KeyError, TypeError, IndexError, EOFError, zipfile.BadZipFile) as exc:
            record['checks'] = [_check('completed_real_artifact_input', 1, 0, detail=str(exc))]
        records.append(record)
    by_group = {}
    for case in cases:
        by_group.setdefault(case['group'], []).append(case)
    translation_checked = False
    for group in by_group.values():
        if group[0]['parameter'] != 'ridge_shift_nm':
            continue
        reference_case = next((c for c in group if float(c['value']) == 0), None)
        for case in group:
            if case is reference_case:
                continue
            record = next(r for r in records if r['case_id'] == case['id'])
            if reference_case is None or reference_case['id'] not in loaded or case['id'] not in loaded:
                record['checks'].append(_check('translation_inputs_complete', 1, 0,
                                               detail='Both current DONE zero-shift and translated cases are required.'))
                continue
            base_model = dict(reference_case['model'])
            moved_model = dict(case['model'])
            base_model.pop('ridge_shift_nm', None)
            moved_model.pop('ridge_shift_nm', None)
            if base_model != moved_model:
                record['checks'].append(_check('translation_single_variable', 1, 0))
                continue
            record['checks'].extend(check_translation(reference_case, loaded[reference_case['id']],
                                                       case, loaded[case['id']], tolerances))
            translation_checked = True
    coverage = {
        'air_interface': any(c['model'].get('geometry_kind') == 'planar_interface'
                             and c['model'].get('n_substrate') == 1 for c in cases),
        'n15_interface': any(c['model'].get('geometry_kind') == 'planar_interface'
                             and c['model'].get('n_substrate') == 1.5 for c in cases),
        'symmetric_ah53': any(c['model'].get('geometry_kind', 'ah53') == 'ah53'
                              and c['model'].get('ridge_shift_nm', 0) == 0
                              and c['model'].get('ridge_center_offsets_nm', [0, 0, 0]) == [0, 0, 0]
                              and c['model'].get('ridge_top_left_inset_nm', 0) == c['model'].get('ridge_top_right_inset_nm', 0)
                              for c in cases),
        'rigid_translation': translation_checked,
    }
    for record in records:
        record['status'] = 'FAIL' if any(c['status'] == 'FAIL' for c in record['checks']) else 'PASS'
    passed = bool(records) and all(coverage.values()) and all(r['status'] == 'PASS' for r in records)
    report = {
        'schema_version': '1.0',
        'evaluated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds'),
        'status': 'PASS' if passed else 'FAIL',
        'solver_fingerprint': fingerprint,
        'scope': 'Minimum FDTD air/Fresnel/symmetry/translation benchmarks; mesh convergence and FDTD/RCWA equivalence are separate gates.',
        'source_policy': 'Only existing current-DONE local FDTD artifacts are read; no simulation data or calibration metadata is generated or changed.',
        'physical_conventions': {
            'coordinates': 'X periodic; Y height; invariant Z; incident -Y',
            'polarization': 's=+Z; normal incident and reflected p=+X',
            'reference': 'r_E is outgoing/incident complex E at the fixed model.sim.reference_y_um',
            'fresnel': 'r_E=((1-n)/(1+n))*exp(-2i*sign_in*k0*(y_ref-y_interface))',
            'fourier': 'integral(E(x)*exp(-i*2*pi*m*x/P))/P',
            'translation': 'r_m(dx)/r_m(0)=exp(-i*2*pi*m*dx/P); +/- pair change=-4*pi*m*dx/P',
            'power': 'r_power=sqrt(cos(theta))*r_E; eta=cos(theta)*sum_output|r_E|^2 for reflected air ports',
        },
        'tolerances': tolerances,
        'coverage': coverage,
        'cases': records,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / 'physics_validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
    body = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AH53物理基准</title>',
            '<style>body{font:15px/1.6 system-ui;max-width:1200px;margin:30px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{padding:6px;text-align:left;border-bottom:1px solid #ddd}.FAIL{color:#b00}.SUPPRESSED{color:#666}</style>',
            f'<h1>AH53 最小物理基准：{report["status"]}</h1>',
            '<p>仅读取真实完成的本机 FDTD 结果；缺失或不完整数据明确拒绝。通过本页不代表网格收敛或 FDTD/RCWA 相互校准已经完成。</p>',
            '<p>相位参考面保留固定位置。空气/n=1.5 界面在界面处两个同向 p 基和 s 基的复反射均为 -0.2；在参考面处按实际测得的入射相位符号传播。低于振幅阈值的相位不解释。</p>',
            '<p><a href="physics_validation.json">完整验收值、阈值、物理约定与拒绝理由</a></p>',
            '<pre>' + html.escape(json.dumps({'coverage': coverage, 'tolerances': tolerances}, ensure_ascii=False, indent=2)) + '</pre>']
    for record in records:
        body.append('<h2>' + html.escape(record['case_id']) + '</h2><table><tr><th>检查</th><th>结论</th><th>误差/值</th><th>上限</th><th>说明</th></tr>')
        for check in record['checks']:
            body.append('<tr class="' + check['status'] + '"><td>' + html.escape(check['name']) + '</td><td>' + check['status'] + '</td><td>' + (f'{check["value"]:.9g}' if check['value'] is not None else '—') + '</td><td>' + f'{check["limit"]:.9g}' + '</td><td>' + html.escape(check['detail']) + '</td></tr>')
        body.append('</table>')
    body.append('</html>')
    (output / 'physics_validation.html').write_text('\n'.join(body), encoding='utf8')
    return report, output
