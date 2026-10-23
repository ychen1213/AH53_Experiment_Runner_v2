"""Validate complete solver artifacts before resuming or publishing a report."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from .materials import (EFFECTIVE_ABSORPTION_K_THRESHOLD, is_effectively_absorbing,
                        material_spec, fit_range_nm, verify_material_databases)


class ArtifactError(ValueError):
    """An output is missing, inconsistent, or belongs to another execution."""


def strict_json_loads(value):
    def reject_constant(constant):
        raise ValueError(f'Nonfinite JSON constant: {constant}')
    return json.loads(value, parse_constant=reject_constant)


def solver_fingerprint():
    digest = hashlib.sha256()
    for name in ('solver.py', 'geometry.py', 'materials.py'):
        digest.update(name.encode('utf8'))
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()[:16]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _require(condition, message):
    if not condition:
        raise ArtifactError(message)


def _json(path):
    try:
        value = strict_json_loads(Path(path).read_text(encoding='utf8'))
        _require(isinstance(value, dict), f'{Path(path).name}: expected an object')
        return value
    except (OSError, ValueError) as exc:
        raise ArtifactError(f'{Path(path).name}: {exc}') from exc


def _array(data, key, shape):
    _require(key in data, f'result.npz: missing {key}')
    value = np.asarray(data[key])
    _require(value.shape == shape, f'{key}: shape {value.shape}, expected {shape}')
    _require(np.issubdtype(value.dtype, np.number), f'{key}: not numeric')
    _require(np.isfinite(value).all(), f'{key}: nonfinite values')
    return value


def _close(actual, expected, name):
    _require(np.allclose(actual, expected, rtol=1e-6, atol=1e-10),
             f'{name}: inconsistent derived values')


def _material_evidence(folder, case, metadata):
    model = case['model']
    verify_material_databases(model)
    materials = metadata.get('materials')
    required = (bool(model.get('material_databases'))
                or 'substrate_material' in model or 'ridge_material' in model
                or any('material' in film for film in model.get('films', [])))
    if materials is None and not required:
        return
    _require(isinstance(materials, dict), 'missing material resolution provenance')
    material_file = folder/'material_resolution.json'
    _require(metadata.get('material_resolution_sha256') == file_sha256(material_file),
             'material resolution checksum mismatch or missing')
    _require(_json(material_file) == materials, 'material resolution metadata mismatch')
    _require(materials.get('material_databases') == model.get('material_databases', []),
             'material database provenance differs from frozen inputs')
    _require(materials.get('schema_version') == '1.0', 'unsupported material evidence schema')
    _require(materials.get('material_checks', {}).get('status') == 'PASS',
             'material preflight checks did not pass')
    _close(materials.get('fit_wavelength_range_nm'), fit_range_nm(model), 'material fit range')
    _close(materials.get('target_wavelength_nm'), model['wavelength_nm'], 'material target wavelength')
    # Geometry determines the actually used roles, including planarized t=0 fill.
    from .geometry import build_geometry
    shapes, _, _ = build_geometry(model)
    expected_roles = {shape.material for shape in shapes}
    roles = materials.get('roles', {})
    _require(set(roles) == expected_roles, 'resolved material roles differ from geometry')
    for role in expected_roles:
        spec = material_spec(model, role)
        actual = roles[role]
        _require(actual.get('kind') == spec['kind'], f'{role}: material kind mismatch')
        if spec['kind'] == 'database':
            _require(actual.get('name') == spec['name'], f'{role}: material name mismatch')
            _require(actual.get('validation', {}).get('status') == 'PASS',
                     f'{role}: named material checks did not pass')
        for key in ('n', 'k', 'raw_n', 'raw_k'):
            value = actual.get(key)
            _require(isinstance(value, (int, float)) and np.isfinite(value),
                     f'{role}: missing/nonfinite actual material {key}')
        _require(actual['n'] >= -1e-12 and actual['k'] >= -1e-12
                 and actual['raw_n'] >= -1e-12 and actual['raw_k'] >= -1e-12,
                 f'{role}: invalid/nonpassive actual material index')
        if spec['kind'] == 'constant':
            _close(actual['n'], spec['n'], f'{role} constant n')
            _close(actual['k'], 0, f'{role} constant k')


def load_artifacts(folder, case, require_done=True):
    """Read and validate everything, returning data only after all checks pass.

    This checks provenance and numerical consistency; it does not certify physical
    convergence or Fresnel calibration. Legacy artifacts without provenance must
    be retained and recomputed, never silently accepted.
    """
    folder = Path(folder)
    try:
        frozen = _json(folder / 'case.json')
        for key in ('id', 'digest', 'model', 'solver_fingerprint', 'capture_field'):
            _require(frozen.get(key) == case.get(key), f'frozen case {key} mismatch')
        if require_done:
            status = _json(folder / 'status.json')
            _require(status.get('status') == 'DONE', 'status is not DONE')
            _require(status.get('digest') == case['digest'], 'status digest mismatch')
            _require(status.get('solver_fingerprint') == case.get('solver_fingerprint'),
                     'status solver fingerprint mismatch')
        fingerprint = case.get('solver_fingerprint')
        _require(bool(fingerprint), 'case has no solver fingerprint')
        metadata = _json(folder / 'physics_metadata.json')
        for key in ('case_id', 'digest', 'solver_fingerprint'):
            expected = case['id'] if key == 'case_id' else case.get(key)
            _require(metadata.get(key) == expected, f'metadata {key} mismatch')
        _material_evidence(folder, case, metadata)
        result_path = folder / 'result.npz'
        _require(metadata.get('result_sha256') == file_sha256(result_path),
                 'result.npz checksum mismatch or missing')
        _require((folder / 'AH53_model.fsp').is_file() and
                 (folder / 'AH53_model.fsp').stat().st_size > 0, 'missing AH53_model.fsp')
        with np.load(result_path, allow_pickle=False) as saved:
            result = {key: saved[key].copy() for key in saved.files}
        _require('orders' in result, 'missing orders')
        orders = np.asarray(result['orders'])
        _require(orders.ndim == 1 and orders.size > 0 and
                 np.issubdtype(orders.dtype, np.integer), 'invalid orders')
        _require(np.all(np.diff(orders) > 0), 'orders are not strictly increasing')
        count = orders.size
        jones = _array(result, 'r_E', (count, 2, 2))
        power = _array(result, 'r_power', (count, 2, 2))
        theta = _array(result, 'theta_deg', (count,))
        eta_s = _array(result, 'eta_s', (count,))
        eta_p = _array(result, 'eta_p', (count,))
        wavelength = _array(result, 'lambda_nm', (1,))
        _require(wavelength[0] > 0, 'nonpositive wavelength')
        requested = case['model'].get('wavelength_nm')
        if requested is not None:
            _close(wavelength[0], requested, 'requested/selected wavelength')
        period = case['model']['period_nm']
        maximum = int(np.floor(period / wavelength[0] - 1e-12))
        _require(np.array_equal(orders, np.arange(-maximum, maximum + 1)),
                 'missing, extra, or nonpropagating diffraction orders')
        expected_theta = np.rad2deg(np.arcsin(orders * wavelength[0] / period))
        _close(theta, expected_theta, 'theta_deg')
        cosine = np.cos(np.deg2rad(theta))
        _close(power, jones * np.sqrt(cosine)[:, None, None], 'r_power')
        _close(eta_s, cosine * np.sum(np.abs(jones[:, :, 0]) ** 2, axis=1), 'eta_s')
        _close(eta_p, cosine * np.sum(np.abs(jones[:, :, 1]) ** 2, axis=1), 'eta_p')
        _require(np.all(eta_s >= 0) and np.all(eta_p >= 0), 'negative power fractions')
        _require('quality_json' in result and result['quality_json'].shape == (1,),
                 'missing or malformed quality_json')
        quality = strict_json_loads(str(result['quality_json'][0]))
        _require(isinstance(quality, dict), 'quality_json is not an object')
        numeric = ('selected_lambda_nm', 'total_reflected_s', 'total_reflected_p',
                   'total_transmitted_s', 'total_transmitted_p', 'sum_eta_s', 'sum_eta_p',
                   'flux_decomposition_error_s', 'flux_decomposition_error_p',
                   'power_closure_s', 'power_closure_p', 'incident_spatial_phase_sign')
        for key in numeric:
            _require(key in quality and isinstance(quality[key], (int, float)) and
                     np.isfinite(quality[key]), f'quality: missing/nonfinite {key}')
        _require(quality['incident_spatial_phase_sign'] in (-1, 1), 'invalid incident phase sign')
        _close(quality['selected_lambda_nm'], wavelength[0], 'selected wavelength')
        for pol, eta in (('s', eta_s), ('p', eta_p)):
            reflected = quality['total_reflected_' + pol]
            transmitted = quality['total_transmitted_' + pol]
            _require(reflected >= 0 and transmitted >= 0, 'negative monitor power')
            _close(quality['sum_eta_' + pol], np.sum(eta), f'sum_eta_{pol}')
            _close(quality['power_closure_' + pol], reflected + transmitted, f'closure_{pol}')
            _close(quality['flux_decomposition_error_' + pol], abs(reflected - np.sum(eta)),
                   f'flux discrepancy_{pol}')
            if 'materials' in metadata:
                _close(quality.get('energy_residual_' + pol), 1-reflected-transmitted,
                       f'energy residual_{pol}')
                for flux_name in ('reflection', 'transmission'):
                    flux = quality.get(f'signed_{flux_name}_flux_{pol}')
                    _require(isinstance(flux, (int, float)) and np.isfinite(flux),
                             f'missing/nonfinite signed {flux_name} flux_{pol}')
                    expected_flux = reflected if flux_name == 'reflection' else transmitted
                    _close(abs(flux), expected_flux, f'signed {flux_name} magnitude_{pol}')
        if 'materials' in metadata:
            absorbing = any(is_effectively_absorbing(role) for role in metadata['materials']['roles'].values())
            _require(quality.get('absorption_present') is absorbing, 'material absorption flag mismatch')
            _require(quality.get('absorption_k_threshold') == EFFECTIVE_ABSORPTION_K_THRESHOLD,
                     'absorption classification threshold mismatch')
            _close(quality.get('maximum_fitted_k'),
                   max(role['k'] for role in metadata['materials']['roles'].values()),
                   'maximum actual fitted k')
            _require(quality.get('energy_check_kind') == (
                'passive_flux_bound' if absorbing else 'lossless_flux_closure'),
                'energy check semantics mismatch')
            planes = metadata.get('power_monitor_planes', {})
            for kind in ('reflection', 'transmission'):
                _close(planes.get(kind+'_y_um'), case['model']['sim'][kind+'_y_um'],
                       kind+' flux plane')
        _require(metadata.get('quality') == quality, 'metadata quality mismatch')
        _require(isinstance(metadata.get('jones_phase_status'), str), 'missing phase calibration status')
        nearfields = {}
        if case.get('capture_field'):
            _require(not quality.get('nearfield_warnings'), 'requested nearfield extraction failed')
            for pol in ('s', 'p'):
                with np.load(folder / f'nearfield_{pol}.npz', allow_pickle=False) as field:
                    x, y, e2 = (np.array(field[key]) for key in ('x_um', 'y_um', 'E2'))
                _require(x.ndim == y.ndim == 1 and len(x) >= 2 and len(y) >= 2,
                         f'{pol} nearfield coordinates invalid')
                _require(np.isfinite(x).all() and np.isfinite(y).all() and
                         np.all(np.diff(x) > 0) and np.all(np.diff(y) > 0),
                         f'{pol} nearfield coordinates not finite/increasing')
                _require(e2.shape == (len(x), len(y)) and np.isfinite(e2).all() and np.all(e2 >= 0),
                         f'{pol} nearfield E2 invalid')
                nearfields[pol] = {'x_um': x, 'y_um': y, 'E2': e2}
        verify_material_databases(case['model'])
        return {'result': result, 'quality': quality, 'metadata': metadata, 'nearfields': nearfields}
    except ArtifactError:
        raise
    except Exception as exc:
        raise ArtifactError(f'{type(exc).__name__}: {exc}') from exc
