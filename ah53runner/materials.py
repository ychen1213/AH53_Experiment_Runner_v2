"""Frozen MDF inputs and explicit, passive isotropic material resolution.

Geometry previews use only selectors.  Optical constants are obtained from a
real Lumerical session and are never guessed from a material's display name.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np

C0 = 299792458.0
# Classification only: never replace the complex index assigned to a solver.
# For a raw lossless material, this bound corresponds to <= 2.1e-7 intensity
# attenuation over 1.05 um at 643 nm. Actual raw absorption, however small,
# always takes the absorbing-material path.
EFFECTIVE_ABSORPTION_K_THRESHOLD = 1e-8


def is_negligible_fit_loss(material):
    """Small fitted loss on a raw lossless material; keep the actual n/k."""
    fitted_k = _finite(material['k'], 'resolved material.k')
    # Without raw provenance, conservatively treat any positive k as physical.
    raw_k = _finite(material.get('raw_k', fitted_k), 'resolved material.raw_k')
    return raw_k <= 0 and 0 < fitted_k <= EFFECTIVE_ABSORPTION_K_THRESHOLD


def is_effectively_absorbing(material):
    """Retain every actual raw absorber, even below the fit-noise threshold."""
    fitted_k = _finite(material['k'], 'resolved material.k')
    raw_k = _finite(material.get('raw_k', fitted_k), 'resolved material.raw_k')
    return raw_k > 0 or fitted_k > EFFECTIVE_ABSORPTION_K_THRESHOLD


def _finite(value, label, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a finite number.')
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{label} must be a finite number.') from error
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f'{label} must be finite' + (' and > 0.' if positive else '.'))
    return result


def material_spec(model, role):
    """Return a pure selector; named and numeric inputs cannot be mixed."""
    if role in ('substrate', 'ridge'):
        source, name_key, n_key, k_key = model, role+'_material', 'n_'+role, 'k_'+role
        default_n = None
    elif re.fullmatch(r'film_[1-9][0-9]*', role):
        index = int(role[5:])-1
        try:
            source = model.get('films', [])[index]
        except IndexError as error:
            raise ValueError(f'Unknown material role {role}.') from error
        name_key, n_key, k_key, default_n = 'material', 'n', 'k', 1.0
    else:
        raise ValueError(f'Unknown material role {role}.')
    if name_key in source:
        name = source[name_key]
        if not isinstance(name, str) or not name.strip() or any(c in name for c in '\r\n\0'):
            raise ValueError(f'{role}: {name_key} must be a nonempty, exact material name.')
        if n_key in source or k_key in source:
            raise ValueError(f'{role}: material name conflicts with numeric {n_key}/{k_key}; omit the numeric fields.')
        if name in ('<Object defined dielectric>', '<Sampled 2D data>'):
            raise ValueError(f'{role}: {name!r} is not a supported bulk database material.')
        return {'kind': 'database', 'name': name}
    n = _finite(source.get(n_key, default_n), f'{role}.{n_key}', positive=True)
    k = _finite(source.get(k_key, 0.0), f'{role}.{k_key}')
    if abs(k) > 1e-15:
        raise ValueError(f'{role}: numeric constants support real n only; use a named MDF material for absorption.')
    return {'kind': 'constant', 'n': n, 'k': 0.0}


def material_roles(model, *, include_inactive=False):
    # Full runner models must follow actual objects, including stacked zero
    # layers on an already-flat surface. A nonzero configured film count does
    # not establish that any of that material is present in the geometry.
    if not include_inactive and 'period_nm' in model:
        from .geometry import build_geometry
        shapes, unused_top, unused_summary = build_geometry(model)
        return list(dict.fromkeys(shape.material for shape in shapes))
    # Minimal optical-only models used by the material-inspection command do
    # not define geometry. Their selectors explicitly describe the checked roles.
    roles = ['substrate']
    if model.get('geometry_kind', 'ah53') != 'planar_interface':
        roles.append('ridge')
        for index, film in enumerate(model.get('films', []), 1):
            mode = str(film.get('mode', 'none')).lower()
            if include_inactive or mode == 'planarized' or (mode == 'conformal' and float(film.get('thickness_nm', 0)) > 0):
                roles.append(f'film_{index}')
    return roles


def has_named_materials(model):
    return any(material_spec(model, role)['kind'] == 'database'
               for role in material_roles(model))


def fit_range_nm(model):
    target = _finite(model['wavelength_nm'], 'wavelength_nm', positive=True)
    width = _finite(model.get('sim', {}).get('source_bandwidth_nm', 200.0),
                    'sim.source_bandwidth_nm', positive=True)
    low, high = target-width/2, target+width/2
    if low <= 0:
        raise ValueError('The source/material-fit wavelength range must stay positive; reduce source_bandwidth_nm.')
    return low, high


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def freeze_material_databases(paths, config_directory):
    """Resolve user paths once, before case hashes are calculated."""
    if not isinstance(paths, list):
        raise ValueError('material_databases must be an array of .mdf path strings.')
    records, seen_paths, seen_hashes = [], set(), set()
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Each material_databases entry must be a nonempty .mdf path string.')
        path = Path(value).expanduser()
        path = (path if path.is_absolute() else Path(config_directory)/path).resolve()
        if path.suffix.lower() != '.mdf' or not path.is_file():
            raise ValueError(f'Material database is not an existing .mdf file: {path}')
        digest = sha256_file(path)
        if str(path).casefold() in seen_paths or digest in seen_hashes:
            raise ValueError(f'Duplicate material database (path or identical contents): {path}')
        seen_paths.add(str(path).casefold())
        seen_hashes.add(digest)
        records.append({'path': str(path), 'sha256': digest})
    return records


def verify_material_databases(model):
    records = model.get('material_databases', [])
    if not isinstance(records, list):
        raise ValueError('Frozen model.material_databases must be an array of path/hash records.')
    seen = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {'path', 'sha256'}:
            raise ValueError('Unfrozen material database: use top-level material_databases in the JSON config.')
        path = Path(record['path'])
        digest = record['sha256']
        if not path.is_absolute() or path.suffix.lower() != '.mdf' or not re.fullmatch(r'[0-9a-f]{64}', str(digest)):
            raise ValueError('Invalid frozen MDF path/hash record.')
        if str(path).casefold() in seen:
            raise ValueError(f'Duplicate frozen MDF path: {path}')
        seen.add(str(path).casefold())
        if not path.is_file() or sha256_file(path) != digest:
            raise ValueError(f'Material database is missing or changed after planning: {path}. Replan before running or reusing results.')


def validate_material_config(model):
    fit_range_nm(model)
    for role in material_roles(model, include_inactive=True):
        material_spec(model, role)
    verify_material_databases(model)


def list_material_names(session):
    """Read exact installed/imported names through the documented API."""
    value = session.getmaterial()
    if isinstance(value, str):
        names = [name for name in value.splitlines() if name]
    elif isinstance(value, (list, tuple, np.ndarray)):
        names = list(np.asarray(value, dtype=object).ravel())
    else:
        raise ValueError('Lumerical did not return a readable material-name list.')
    if not names or not all(isinstance(name, str) and name for name in names) or len(names) != len(set(names)):
        raise ValueError('Lumerical material-name list is empty, invalid, or ambiguous.')
    return sorted(names)


def _property_names(session, name):
    value = session.getmaterial(name)
    if not isinstance(value, str):
        raise ValueError(f'{name}: cannot inspect material properties.')
    return {line.strip().lower(): line.strip() for line in value.splitlines() if line.strip()}


def _json_value(value):
    array = np.asarray(value)
    if array.dtype.kind in 'fc' and not np.isfinite(array).all():
        raise ValueError('Nonfinite material property.')
    if np.iscomplexobj(array):
        return {'real': array.real.tolist(), 'imag': array.imag.tolist()}
    return array.tolist()


def material_definition_digest(session, name):
    """Fingerprint readable physical and fitting settings, not a name alone."""
    props = _property_names(session, name)
    definition = {key: _json_value(session.getmaterial(name, original))
                  for key, original in sorted(props.items())}
    encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(',', ':')).encode('utf8')
    return hashlib.sha256(encoded).hexdigest()


def _passive_index(value, size, label):
    array = np.asarray(value, dtype=complex).reshape(-1)
    if array.size != size or not np.isfinite(array).all():
        raise ValueError(f'{label}: incomplete or nonfinite optical-index response.')
    # Positive-k convention of getindex/getfdtdindex. Allow roundoff only.
    if np.any(array.real < -1e-12) or np.any(array.imag < -1e-12):
        raise ValueError(f'{label}: gain/negative-index material is outside the passive isotropic scope.')
    if np.any(np.abs(array) < 1e-12):
        raise ValueError(f'{label}: zero-index singularity is unsupported.')
    return array


def _resolve_one(session, name, model):
    low, high = fit_range_nm(model)
    target = float(model['wavelength_nm'])
    wavelengths = np.unique(np.r_[np.linspace(low, high, 1001), target])
    frequencies = C0/(wavelengths*1e-9)
    fmin, fmax = C0/(high*1e-9), C0/(low*1e-9)
    props = _property_names(session, name)
    metadata = {}
    if 'anisotropy' not in props:
        raise ValueError(f'{name}: cannot establish isotropic bulk material metadata.')
    anisotropy = session.getmaterial(name, props['anisotropy'])
    metadata['anisotropy'] = _json_value(anisotropy)
    if isinstance(anisotropy, str):
        isotropic = anisotropy.lower().strip() in ('isotropic', 'none')
    else:
        scalar = np.asarray(anisotropy)
        isotropic = scalar.size == 1 and float(scalar.reshape(-1)[0]) == 0
    if not isotropic:
        raise ValueError(f'{name}: anisotropic materials are not supported.')
    for prop in ('type', 'material type', 'max coefficients', 'max coefficient', 'tolerance',
                 'make fit passive', 'improve numerical stability', 'fit tolerance', 'imaginary weight',
                 'specify fit range',
                 'frequency min', 'frequency max', 'wavelength min', 'wavelength max'):
        if prop in props:
            metadata[prop] = _json_value(session.getmaterial(name, props[prop]))
    material_type = str(metadata.get('type', metadata.get('material type', ''))).lower()
    supported_types = {'sampled 3d data', 'dielectric', '(n,k) material',
                       'lorentz', 'drude', 'debye', 'conductive', 'sellmeier',
                       'cauchy', 'analytic'}
    if material_type not in supported_types:
        raise ValueError(f'{name}: material type {material_type!r} is outside the supported linear bulk model list.')
    if metadata.get('specify fit range'):
        specified_min = metadata.get('frequency min')
        specified_max = metadata.get('frequency max')
        if specified_min is None or specified_max is None or not np.allclose(
                [specified_min, specified_max], [fmin, fmax], rtol=1e-10, atol=0):
            raise ValueError(f'{name}: the material overrides the fit range; disable Specify fit range in the MDF or match the configured source band.')
    # Sampled data must cover the complete source band; no implicit extrapolation.
    sampled_range = None
    sampled_property = next((props[key] for key in ('sampled 3d data', 'sampled data') if key in props), None)
    if material_type == 'sampled 3d data' and sampled_property is None:
        raise ValueError(f'{name}: sampled material has no inspectable raw data.')
    if sampled_property is not None:
        sampled = np.asarray(session.getmaterial(name, sampled_property), dtype=complex)
        if sampled.ndim != 2 or sampled.shape[1] != 2 or sampled.shape[0] < 2 or not np.isfinite(sampled).all():
            raise ValueError(f'{name}: expected finite isotropic sampled data with frequency and epsilon columns.')
        raw_frequency = sampled[:, 0]
        if np.any(raw_frequency.real <= 0) or np.any(np.abs(raw_frequency.imag) > 0):
            raise ValueError(f'{name}: invalid sampled-data frequencies.')
        sampled_range = [float(raw_frequency.real.min()), float(raw_frequency.real.max())]
        if fmin < sampled_range[0]*(1-1e-12) or fmax > sampled_range[1]*(1+1e-12):
            raise ValueError(f'{name}: MDF sampled-data range does not cover the full {low:g}–{high:g} nm source/material-fit band.')
        if np.any(sampled[:, 1].imag < -1e-12):
            raise ValueError(f'{name}: sampled permittivity contains gain; passive materials only.')
        metadata['sampled_data_sha256'] = hashlib.sha256(np.ascontiguousarray(sampled).tobytes()).hexdigest()
    raw = _passive_index(session.getindex(name, frequencies), len(frequencies), name+' raw')
    fitted = _passive_index(session.getfdtdindex(name, frequencies, fmin, fmax), len(frequencies), name+' fitted')
    # Metadata alone is insufficient: verify all Cartesian responses too.
    for component in (2, 3):
        raw_axis = _passive_index(session.getindex(name, frequencies, component), len(frequencies), name+' raw axis')
        fit_axis = _passive_index(session.getfdtdindex(name, frequencies, fmin, fmax, component), len(frequencies), name+' fit axis')
        if not np.allclose(raw_axis, raw, rtol=1e-10, atol=1e-12) or not np.allclose(fit_axis, fitted, rtol=1e-10, atol=1e-12):
            raise ValueError(f'{name}: Cartesian material responses are not isotropic.')
    relative_error = np.abs(fitted**2-raw**2)/np.maximum(np.abs(raw**2), 1e-12)
    settings = model.get('material_fit', {})
    if not isinstance(settings, dict):
        raise ValueError('material_fit must be an object.')
    threshold = _finite(settings.get('max_relative_permittivity_error', 0.05),
                        'material_fit.max_relative_permittivity_error', positive=True)
    if threshold > 1:
        raise ValueError('material_fit.max_relative_permittivity_error must be <= 1.')
    if float(relative_error.max()) > threshold:
        raise ValueError(f'{name}: material-fit relative epsilon error {relative_error.max():.6g} exceeds {threshold:g}; improve the material fit or narrow source_bandwidth_nm.')
    index = int(np.flatnonzero(wavelengths == target)[0])
    return {'kind': 'database', 'name': name,
            'n': float(fitted[index].real), 'k': float(fitted[index].imag),
            'raw_n': float(raw[index].real), 'raw_k': float(raw[index].imag),
            'model': 'Lumerical multi-coefficient fit (getfdtdindex)',
            'fit_properties': metadata,
            'sampled_frequency_range_hz': sampled_range,
            'validation': {'status': 'PASS', 'scope': 'passive isotropic bulk; sampled spectral checks',
                           'max_relative_permittivity_error': float(relative_error.max()),
                           'relative_permittivity_error_limit': threshold,
                           'target_relative_permittivity_error': float(relative_error[index]),
                           'spectrum_samples': len(wavelengths), 'cartesian_components_checked': [1, 2, 3]},
            'spectrum': {'wavelength_nm': wavelengths.tolist(),
                         'raw_n': raw.real.tolist(), 'raw_k': raw.imag.tolist(),
                         'n': fitted.real.tolist(), 'k': fitted.imag.tolist()}}


def import_material_databases(session, model):
    """Import frozen inputs and audit actual changes to selected definitions.

An MDF may intentionally replace a built-in definition. Multiple MDFs may not
replace the same selected definition with different values: that would make
the optical response depend on an easily missed library ordering choice.
"""
    verify_material_databases(model)
    imports = []
    selected = {spec['name'] for role in material_roles(model)
                if (spec := material_spec(model, role))['kind'] == 'database'}
    changed_by = {}
    for record in model.get('material_databases', []):
        before = set(list_material_names(session))
        prior = {name: material_definition_digest(session, name)
                 for name in selected.intersection(before)}
        session.importmaterialdb(record['path'])
        after = set(list_material_names(session))
        added = sorted(after-before)
        if not before.issubset(after):
            raise ValueError('Material import unexpectedly removed existing material names.')
        changes = {}
        for name in sorted(selected.intersection(after)):
            current = material_definition_digest(session, name)
            if prior.get(name) != current:
                if name in changed_by:
                    raise ValueError(f'Material {name!r} is changed by both {changed_by[name]} and {record["path"]}; use one unambiguous MDF definition.')
                changed_by[name] = record['path']
                changes[name] = {'before_sha256': prior.get(name), 'after_sha256': current,
                                 'overrode_existing_definition': name in before}
        imports.append(dict(record, imported_names=added, selected_material_changes=changes))
    verify_material_databases(model)
    return imports


def resolve_materials(session, model):
    """Import MDFs and resolve every used role before any structure is built."""
    validate_material_config(model)
    imports = import_material_databases(session, model)
    specifications = {role: material_spec(model, role) for role in material_roles(model)}
    names = set(list_material_names(session)) if has_named_materials(model) else set()
    resolved, unique = {}, {}
    for role, spec in specifications.items():
        if spec['kind'] == 'constant':
            resolved[role] = dict(spec, raw_n=spec['n'], raw_k=0.0, model='constant real index')
            continue
        name = spec['name']
        if name not in names:
            raise ValueError(f'{role}: material {name!r} was not found after MDF import. Use tools/materials.py list to copy its exact name.')
        if name not in unique:
            unique[name] = _resolve_one(session, name, model)
            unique[name]['definition_sha256'] = material_definition_digest(session, name)
        resolved[role] = copy.deepcopy(unique[name])
    verify_material_databases(model)
    for material in resolved.values():
        material['absorption_classification'] = {
            'effectively_absorbing': is_effectively_absorbing(material),
            'negligible_fit_loss': is_negligible_fit_loss(material),
            'fit_loss_k_threshold': EFFECTIVE_ABSORPTION_K_THRESHOLD,
            'rule': 'Any positive raw k is absorbing; only raw lossless materials may classify fitted k <= threshold as negligible.',
            'actual_n_k_preserved': True}
    return {'schema_version': '1.0', 'roles': resolved,
            'material_databases': copy.deepcopy(model.get('material_databases', [])),
            'fit_wavelength_range_nm': list(fit_range_nm(model)),
            'target_wavelength_nm': float(model['wavelength_nm']),
            'imported_names': sorted(name for item in imports for name in item['imported_names']),
            'imports': imports,
            'material_checks': {'status': 'PASS', 'unique_named_materials': len(unique),
                                'import_policy': 'MDF may override builtins; multiple changed definitions for a selected name are rejected',
                                'optical_model': 'same FDTD multi-coefficient fit range for both backends',
                                'spectral_passivity_is_sampled_not_a_continuous_frequency_proof': True}}
