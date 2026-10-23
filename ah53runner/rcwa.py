"""Isolated, conservative RCWA adapter for the shared lossless rectangular geometry.

Native acquisitions are research data, never calibrated results. Conversion to
the common S11/S21 and electric-amplitude interface requires a certificate made
from this backend's own planar/symmetry/translation acquisitions at two meshes,
plus independent weak-scattering controls of the absolute transverse origin.
No complete two-sided multi-order S matrix is claimed.
"""
from __future__ import annotations

from dataclasses import asdict
import copy
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .geometry import build_geometry
from .materials import (has_named_materials, material_spec, fit_range_nm,
                        verify_material_databases, is_effectively_absorbing,
                        EFFECTIVE_ABSORPTION_K_THRESHOLD)

ROTATION = np.array([[1., 0., 0.], [0., 0., 1.], [0., -1., 0.]])
DATASETS = ('grating_characterization', 'field_amplitudes', 'grating_orders',
            'grating_power', 'total_energy', 'substrate', 'index')
REGISTRATIONS = ('native', 'half_cell', 'cell_origin_half_cell')
ORIGIN_TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'validate_rcwa_origin.py'


class RCWAError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise RCWAError(message)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def backend_fingerprint():
    digest = hashlib.sha256()
    # The independent Born evaluator is part of the export gate's mathematics.
    # Changing it requires real reacquisition, just like changing this adapter.
    for path in (Path(__file__), Path(__file__).with_name('geometry.py'),
                 Path(__file__).with_name('materials.py'), ORIGIN_TOOL):
        digest.update(path.name.encode('utf8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode('utf8')).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')
    temporary.replace(path)


def geometry_spec(model):
    """Rotate the actual shared Shape objects, preserving materials and interfaces."""
    require(float(model['sim']['reference_y_um']) == 1.8,
            'Phase-one RCWA requires the shared fixed reference_y_um=1.8.')
    require(float(model['sim']['transmission_y_um']) == -1.05,
            'Phase-one RCWA requires fixed transmission_y_um=-1.05.')
    require(not model.get('ridge_top_left_inset_nm', 0)
            and not model.get('ridge_top_right_inset_nm', 0),
            'Sloped sidewalls are not supported by the rectangular RCWA adapter.')
    for film in model.get('films', []):
        require(float(film.get('k', 0)) == 0, 'Only real, lossless, constant film indices are supported.')
    shapes, top, metadata = build_geometry(model)
    invariant_span_nm = float(model['sim'].get('stripe_z_span_um',1.0))*1000.
    require(math.isfinite(invariant_span_nm) and invariant_span_nm>0, 'Invalid invariant-axis bookkeeping span.')
    boxes = []
    for shape in shapes:
        require(not shape.vertices_nm, 'Polygon/sloped geometry is unsupported; no rectangle substitution is allowed.')
        values = (shape.x1_nm, shape.x2_nm, shape.y1_nm, shape.y2_nm)
        require(all(math.isfinite(float(value)) for value in values), 'Nonfinite shared geometry.')
        named = getattr(shape, 'material_name', None)
        require(named or (shape.index_n is not None and math.isfinite(shape.index_n) and shape.index_n > 0),
                'Indices must be positive real constants or an explicit database material.')
        box = {'name': shape.name, 'material': shape.material,
                      'n': None if named else float(shape.index_n),
                      'x_min_nm': float(shape.x1_nm), 'x_max_nm': float(shape.x2_nm),
                      'y_min_nm': -invariant_span_nm/2, 'y_max_nm': invariant_span_nm/2,
                      'z_min_nm': -float(shape.y2_nm), 'z_max_nm': -float(shape.y1_nm)}
        if named:
            box['material_name'] = named
        boxes.append(box)
    reference = 1800.
    transmitted = 1050.
    require(top < reference, 'Geometry intersects the fixed incident/reflection reference plane.')
    interfaces = sorted({-reference, transmitted} | {
        edge for box in boxes for edge in (box['z_min_nm'], box['z_max_nm'])
        if -reference < edge < transmitted})
    sim = model['sim']
    zmin, zmax = -1000. * float(sim['y_max_um']), -1000. * float(sim['y_min_um'])
    require(zmin < -reference < transmitted < zmax,
            'RCWA simulation region must enclose the fixed outer interfaces.')
    spec = {'geometry_source': 'ah53runner.geometry.build_geometry',
            'rotation_original_to_rcwa': ROTATION.tolist(),
            'coordinate_map': '(x,y,z)_AH53 -> (x,z,-y)_RCWA; proper rotation det=+1',
            'period_nm': float(model['period_nm']), 'wavelength_nm': float(model['wavelength_nm']),
            'n_incident': 1., 'n_substrate': material_spec(model, 'substrate').get('n'),
            'source_reference_z_nm': -reference, 'reflection_reference_z_nm': -reference,
            'transmission_reference_z_nm': transmitted, 'interfaces_nm': interfaces,
            'solver_z_min_nm': zmin, 'solver_z_max_nm': zmax,
            'source_shapes_xy_nm': [asdict(shape) for shape in shapes],
            'boxes': boxes, 'geometry_metadata': metadata}
    if has_named_materials(model):
        spec['material_databases'] = model.get('material_databases', [])
        spec['fit_wavelength_range_nm'] = list(fit_range_nm(model))
    return spec


def validate_settings(settings, model):
    require(set(settings) <= {'name', 'mesh_cells_x', 'max_number_k_vectors', 'conversion_absolute',
                             'substrate_depth_nm', 'termination_index'},
            'Unsupported RCWA settings; refusing to ignore unknown properties.')
    for key in ('mesh_cells_x', 'max_number_k_vectors'):
        value = settings.get(key)
        require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
                f'{key} must be a positive integer.')
    # A named substrate is resolved only in a real CAD session. Planning checks
    # air-port sampling; the resolved terminal port is checked before execution.
    n_substrate = material_spec(model, 'substrate').get('n', 1.)
    maximum = math.floor(float(n_substrate) * float(model['period_nm']) /
                         float(model['wavelength_nm']) + 1e-9)
    require(settings['max_number_k_vectors'] >= 2*maximum+3,
            'Conservative truncation requires both signs of every propagating substrate order plus an evanescent pair; actual native orders are checked separately.')
    require(settings['mesh_cells_x'] >= 2 * settings['max_number_k_vectors'] + 1,
            'Insufficient transverse sampling for the requested Fourier truncation.')
    tolerance = float(settings.get('conversion_absolute', 1e-8))
    require(math.isfinite(tolerance) and 0 < tolerance <= 1e-6,
            'conversion_absolute must be positive and <=1e-6.')
    for name in ('substrate_depth_nm', 'termination_index'):
        if name in settings:
            require(math.isfinite(float(settings[name])) and float(settings[name]) > 0,
                    name + ' must be positive and finite.')
            require(has_named_materials(model), name + ' is only used by the named-material path.')


def resolved_geometry(spec, materials, settings, depth_multiplier=1.):
    """Explicit RCWA representation of the thick physical substrate.

    RCWA discards loss in its final half-space. A lossy substrate is therefore
    an internal layer terminating in a declared real medium; its artificial
    back interface must pass a separately acquired doubled-depth check.
    """
    actual = copy.deepcopy(spec)
    roles = materials['roles']
    for box in actual['boxes']:
        record = roles[box['material']]
        if box.get('material_name'):
            require(record.get('name') == box['material_name'], 'Material resolution/name mismatch.')
        box['n'] = float(record['n'])
        box['k'] = float(record['k'])
    sub = roles['substrate']
    actual['n_substrate'] = float(sub['n'])
    actual['k_substrate'] = float(sub['k'])
    actual['material_resolution'] = copy.deepcopy(materials)
    lossy = is_effectively_absorbing(sub)
    actual['absorbing_substrate'] = lossy
    actual['terminal_index'] = float(sub['n'])
    actual['transmission_scope'] = 'lossless_substrate_reference_plane'
    # The MCM fit of raw lossless data can have tiny positive numerical k.
    # Keep that fitted value in objects/provenance. Only the native final
    # half-space (which cannot represent k) uses its real part, explicitly.
    tiny_fit_loss = not lossy and float(sub['k']) > 0
    actual['terminal_negligible_fit_loss_approximation'] = {
        'applied': tiny_fit_loss, 'fitted_n': float(sub['n']), 'fitted_k': float(sub['k']),
        'raw_k': float(sub.get('raw_k', sub['k'])),
        'fitted_k_threshold': EFFECTIVE_ABSORPTION_K_THRESHOLD,
        'policy': 'Only raw k<=0 and fitted 0<k<=threshold qualifies. Native final half-space uses Re(n); finite layers keep the complete named material. Actual n/k are never overwritten.',
        'single_pass_intensity_loss_over_internal_depth': float(-math.expm1(
            -4*math.pi*float(sub['k'])*spec['transmission_reference_z_nm']/spec['wavelength_nm'])),
        'air_interface_complex_Fresnel_difference': float(abs(
            (1-complex(sub['n'], sub['k']))/(1+complex(sub['n'], sub['k']))
            - (1-float(sub['n']))/(1+float(sub['n']))))}
    if tiny_fit_loss:
        actual['transmission_scope'] = 'substrate_reference_plane_with_declared_negligible_fit_loss_final_halfspace'
    if lossy:
        require(float(sub['k']) > 0,
                'Material data indicates absorption but the fitted target k is not positive; improve the material fit before constructing an absorbing substrate.')
        depth = float(settings.get('substrate_depth_nm', max(
            2 * spec['transmission_reference_z_nm'],
            spec['wavelength_nm'] * math.log(1e10) / (4 * math.pi * float(sub['k'])))))
        depth *= depth_multiplier
        require(depth > spec['transmission_reference_z_nm'],
                'Absorbing substrate must extend below the FDTD internal transmission plane.')
        terminal = float(settings.get('termination_index', 1.))
        substrate_boxes = [b for b in actual['boxes'] if b['material'] == 'substrate']
        require(len(substrate_boxes) == 1, 'Expected one uniform substrate rectangle.')
        substrate_boxes[0]['z_max_nm'] = depth
        termination = dict(substrate_boxes[0], name='rcwa_lossless_termination',
                           material='rcwa_terminal', n=terminal, k=0.,
                           z_min_nm=depth, z_max_nm=depth+1000.)
        termination.pop('material_name', None)
        actual['boxes'].append(termination)
        actual['interfaces_nm'] = sorted(set(spec['interfaces_nm']) | {depth})
        actual['solver_z_max_nm'] = depth + 500.
        actual['transmission_reference_z_nm'] = depth
        actual['internal_fdtd_transmission_z_nm'] = spec['transmission_reference_z_nm']
        actual['substrate_depth_nm'] = depth
        actual['terminal_index'] = terminal
        actual['transmission_scope'] = 'artificial_lossless_terminal_after_absorbing_substrate'
    nout = actual['terminal_index']
    maximum = math.floor(nout * actual['period_nm'] / actual['wavelength_nm'] + 1e-9)
    require(settings['max_number_k_vectors'] >= 2*maximum+3,
            'Resolved terminal index requires more Fourier orders; increase max_number_k_vectors and mesh_cells_x.')
    return actual


def build_lsf(spec, settings, *, new_project=True, run=True):
    """Build one native RCWA project; this function performs no API operation."""
    lines = ['# Shared AH53 geometry, rotated to RCWA z/forward. SI units.']
    if new_project:
        lines.append('newproject;')
        for database in spec.get('material_databases', []):
            lines.append('importmaterialdb(' + json.dumps(database['path'].replace('\\', '/'), ensure_ascii=False) + ');')
    def value(item):
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        if isinstance(item, (list, tuple)):
            if item and isinstance(item[0], (list, tuple)):
                return '['+';'.join(','.join(value(part) for part in row) for row in item)+']'
            return '[' + ','.join(value(part) for part in item) + ']'
        return format(float(item), '.17g')
    def props(properties):
        for key, item in properties.items():
            lines.append(f'set({value(key)},{value(item)});')
    for box in spec['boxes']:
        lines.append('addrect;')
        properties = {'name': box['name']}
        if box.get('material_name'):
            properties['material'] = box['material_name']
        else:
            properties.update(material='<Object defined dielectric>', index=box['n'])
        properties.update({f'{axis} {edge}': box[f'{axis}_{edge}_nm'] * 1e-9
                           for axis in 'xyz' for edge in ('min', 'max')})
        props(properties)
    lines.append('addrcwa;')
    props({'name': 'RCWA', 'simulation region': '2D Y-normal',
           'propagation axis': 'z', 'propagation direction': 'forward',
           'x min': -spec['period_nm'] * .5e-9, 'x max': spec['period_nm'] * .5e-9,
           'y': 0, 'z min': spec['solver_z_min_nm'] * 1e-9,
           'z max': spec['solver_z_max_nm'] * 1e-9,
           'background material': '<Object defined dielectric>', 'index': 1,
           'interface position': 'absolute',
           'interface absolute positions': [edge * 1e-9 for edge in spec['interfaces_nm']],
           'max number k vectors': settings['max_number_k_vectors'],
           'use li factorization': 0, 'mesh type': 'custom',
           'mesh cells x': settings['mesh_cells_x'], 'mesh refinement': 'staircase',
           'fit materials with multi-coefficient model': int('fit_wavelength_range_nm' in spec),
           'incident angle': 'table', 'incident angle table': [[0.,0.]],
           'sample spacing': 'custom',
           'custom frequency samples': [299792458. / (spec['wavelength_nm'] * 1e-9)],
           'report grating characterization': 1, 'report grating orders': 1,
           'report grating power': 1, 'report field amplitudes': 1, 'report index': 1})
    if 'fit_wavelength_range_nm' in spec:
        lo, hi = spec['fit_wavelength_range_nm']
        props({'fit sampled materials': 1, 'fit analytic materials': 1,
               'override materials frequency range': 1,
               'materials minimum wavelength': lo * 1e-9, 'materials maximum wavelength': hi * 1e-9})
    if run:
        lines.append(run_lsf())
    return '\n'.join(lines) + '\n'


def run_lsf():
    lines = ['save("rcwa_model.fsp");', 'run("RCWA");', 'save("rcwa_model.fsp");']
    for name in DATASETS:
        lines.append(f'{name}=getresult("RCWA","{name}");')
    lines.append('matlabsave("native_rcwa.mat",' + ','.join(DATASETS) + ');')
    return '\n'.join(lines) + '\n'


def save_native_dataset(dataset, folder, name):
    """Preserve every native nested value and array without pickle or squeezing."""
    arrays = {}
    def encode(value):
        if isinstance(value, np.ndarray):
            require(not value.dtype.hasobject, 'Cannot archive native object arrays safely.')
            key = f'a{len(arrays):05d}'
            arrays[key] = value
            return {'array': key}
        if isinstance(value, np.generic):
            return encode(value.item())
        if isinstance(value, dict):
            return {'mapping': {key: encode(item) for key, item in value.items()}}
        if isinstance(value, (list, tuple)):
            return {'sequence': [encode(item) for item in value]}
        if isinstance(value, complex):
            return {'complex': [value.real, value.imag]}
        if isinstance(value, float) and not math.isfinite(value):
            return {'nonfinite': str(value)}
        require(value is None or isinstance(value, (str, bool, int, float)), 'Unsupported native value type.')
        return value
    folder = Path(folder)
    tree = encode(dataset)
    np.savez_compressed(folder / f'native_{name}.npz', **arrays)
    write_json(folder / f'native_{name}.json', {'format': 'ah53-native-rcwa-v1', 'tree': tree})


def load_native_dataset(folder, name):
    folder = Path(folder)
    document = json.loads((folder / f'native_{name}.json').read_text(encoding='utf8'))
    require(document.get('format') == 'ah53-native-rcwa-v1', 'Unsupported native archive format.')
    with np.load(folder / f'native_{name}.npz', allow_pickle=False) as archive:
        def decode(value):
            if not isinstance(value, dict):
                return value
            if 'array' in value:
                return np.array(archive[value['array']])
            if 'mapping' in value:
                return {key: decode(item) for key, item in value['mapping'].items()}
            if 'sequence' in value:
                return [decode(item) for item in value['sequence']]
            if 'complex' in value:
                return complex(*value['complex'])
            if 'nonfinite' in value:
                return float(value['nonfinite'])
            raise RCWAError('Malformed native archive tree.')
        return decode(document['tree'])


def _layout(dataset):
    metadata = dataset.get('Lumerical_dataset')
    require(isinstance(metadata, dict), 'Explicit Lumerical dataset metadata is required; axes will not be guessed.')
    groups = metadata.get('parameters')
    require(isinstance(groups, list) and bool(groups), 'Missing parameter axes.')
    axes, shape = {}, []
    for axis, aliases in enumerate(groups):
        if isinstance(aliases, str):
            aliases = [aliases]
        require(isinstance(aliases, (list, tuple)) and bool(aliases), 'Malformed alias group.')
        length = None
        for alias in aliases:
            require(alias not in axes and alias in dataset, 'Duplicate or absent dataset coordinate.')
            vector = np.asarray(dataset[alias])
            require(not vector.dtype.hasobject and sum(size != 1 for size in vector.shape) <= 1,
                    'Dataset coordinates must be scalar/row/column vectors.')
            vector = vector.reshape(-1)
            require(vector.size and np.isfinite(vector).all(), 'Empty or nonfinite dataset coordinates.')
            require(length in (None, len(vector)), 'Coordinate aliases disagree in length.')
            length = len(vector)
            axes[alias] = (axis, vector)
        shape.append(length)
    return axes, tuple(shape), set(metadata.get('attributes', []))


def _order_attributes(dataset, attributes, model):
    axes, shape, listed = _layout(dataset)
    for name in ('lambda', 'theta', 'phi', 'n', 'm'):
        require(name in axes, f'Missing explicit RCWA coordinate: {name}')
    require(axes['n'][0] != axes['m'][0], 'Diffraction orders n/m must have independent axes.')
    for axis, size in enumerate(shape):
        require(axis == axes['n'][0] or size == 1, 'Only one wavelength, one normal ray and invariant-y RCWA are supported.')
    for name, expected in (('lambda', float(model['wavelength_nm']) * 1e-9), ('theta', 0.), ('phi', 0.), ('m', 0.)):
        require(np.allclose(axes[name][1], expected, rtol=1e-10, atol=1e-14), f'Unexpected native {name} coordinate.')
    orders = axes['n'][1]
    require(np.all(orders == np.round(orders)) and np.all(np.diff(orders) > 0), 'Orders must be sorted unique integers.')
    index = tuple(slice(None) if axis == axes['n'][0] else 0 for axis in range(len(shape)))
    result = {}
    for name in attributes:
        require(name in listed and name in dataset, f'Missing native attribute {name}.')
        array = np.asarray(dataset[name])
        require(array.shape == shape and not array.dtype.hasobject and np.isfinite(array).all(),
                f'Invalid native shape/values for {name}; dimensions will not be squeezed or guessed.')
        result[name] = array[index]
    return orders.astype(int), result


def registration_factor(orders, period_nm, mesh_cells_x, registration):
    require(registration in REGISTRATIONS, 'Unknown transverse registration hypothesis.')
    offset = 0. if registration == 'native' else float(period_nm) / (2 * int(mesh_cells_x))
    if registration == 'cell_origin_half_cell':
        # Native DFT sample j=0 is physically xmin+dx/2. This fixed coordinate
        # conversion is accepted only after independent absolute Born controls.
        offset -= float(period_nm) / 2
    return np.exp(-2j * np.pi * np.asarray(orders) * offset / float(period_nm))


def validate_native_transverse_origin(dataset, model, settings):
    """Check the real archived sample coordinates; never infer or move a grid."""
    metadata = dataset.get('Lumerical_dataset', {}) if isinstance(dataset, dict) else {}
    require(metadata.get('geometry') == 'rectilinear', 'Native index needs explicit rectilinear coordinates.')
    x = np.asarray(dataset.get('x', []))
    require(not x.dtype.hasobject and sum(size != 1 for size in x.shape) <= 1
            and np.isfinite(x).all() and np.max(np.abs(x.imag), initial=0) < 1e-15,
            'Invalid native index x coordinate.')
    x = x.real.reshape(-1)
    count = settings['mesh_cells_x']
    period = float(model['period_nm']) * 1e-9
    expected = -period/2 + (np.arange(count)+.5)*period/count
    require(x.shape == expected.shape and np.allclose(x, expected, rtol=1e-10, atol=1e-15),
            'Native index x samples differ from the declared xmin+(j+1/2)dx origin; absolute phase registration is refused.')
    return {'mesh_cells_x': count, 'first_x_nm': float(x[0]*1e9),
            'last_x_nm': float(x[-1]*1e9), 'cell_width_nm': period/count*1e9,
            'coordinate_rule': 'x_j=-period/2+(j+1/2)*period/mesh_cells_x'}


def _total_energy(dataset, name, model):
    axes, shape, listed = _layout(dataset)
    require(all(size == 1 for size in shape) and name in listed and name in dataset,
            'Expected a single-frequency/angle total_energy attribute.')
    require('lambda' in axes and np.allclose(axes['lambda'][1], float(model['wavelength_nm'])*1e-9,
                                            rtol=1e-10, atol=1e-14), 'Total-energy wavelength mismatch.')
    array = np.asarray(dataset[name])
    require(array.shape == shape and np.isfinite(array).all() and np.max(np.abs(array.imag)) < 1e-12,
            'Invalid native total energy.')
    return float(array.real.reshape(-1)[0])


def convert_native(datasets, model, settings, registration='native', resolved_spec=None):
    """Convert raw data in memory for calibration diagnostics; never certifies phase."""
    validate_settings(settings, model)
    geometry_spec(model)
    if has_named_materials(model):
        require(isinstance(resolved_spec, dict) and 'material_resolution' in resolved_spec,
                'Named-material conversion requires the actual resolved material model and terminal medium.')
        terminal_n = float(resolved_spec['terminal_index'])
        absorbing = any(is_effectively_absorbing(role)
                        for role in resolved_spec['material_resolution']['roles'].values())
    else:
        terminal_n, absorbing = float(model['n_substrate']), False
    names = tuple(prefix + a + b for prefix in 'RT' for a in 'sp' for b in 'sp')
    orders, gc = _order_attributes(datasets['grating_characterization'], names, model)
    order2, go = _order_attributes(datasets['grating_orders'], ('kx', 'ky', 'Rs_grating', 'Rp_grating', 'Ts_grating', 'Tp_grating'), model)
    field_names = tuple(f'ek{axis}_{direction}{pol}' for axis in 'xyz' for direction in 'fb' for pol in 'sp')
    order3, fields = _order_attributes(datasets['field_amplitudes'], field_names, model)
    require(np.array_equal(orders, order2) and np.array_equal(orders, order3), 'Native dataset order axes disagree.')
    wavelength, period = float(model['wavelength_nm']), float(model['period_nm'])
    kx, ky = np.asarray(go['kx']).real, np.asarray(go['ky']).real
    require(np.max(np.abs(np.asarray(go['kx']).imag)) < 1e-12
            and np.max(np.abs(np.asarray(go['ky']).imag)) < 1e-12
            and np.allclose(kx, orders * wavelength / period, atol=1e-10)
            and np.max(np.abs(ky)) < 1e-12, 'Unexpected transverse wavevectors.')
    output = {'lambda_nm': np.array([wavelength])}
    diagnostics = {'phase_status': 'UNCALIBRATED_DIAGNOSTIC_ONLY',
                   'registration_hypothesis': registration, 's_matrix_scope': 'S11/S21 columns for top incident zero order, s/p only',
                   'native_to_cartesian_max_error': {}, 'total_power': {}}
    din = np.diag([-1., 1.])
    for prefix, side, nout, sign, direction in (('R', 'r', 1., -1, 'b'),
                                               ('T', 't', terminal_n, 1, 'f')):
        q2 = nout ** 2 - kx ** 2
        propagating = q2 > 1e-12
        selected = orders[propagating]
        maximum = math.floor(nout * period / wavelength - 1e-12)
        require(np.array_equal(selected, np.arange(-maximum, maximum + 1)),
                'Native Fourier truncation omits propagating channels.')
        q = np.sqrt(q2[propagating])
        khat = np.stack([kx[propagating] / nout, np.zeros(len(q)), sign * q / nout], axis=-1)
        native_s = np.stack([np.zeros(len(q)), -np.where(kx[propagating] < -1e-14, -1., 1.), np.zeros(len(q))], axis=-1)
        native_p = np.cross(khat, native_s)
        desired_s = np.tile([0., 1., 0.], (len(q), 1))
        desired_p = np.stack([q / nout, np.zeros(len(q)), -sign * kx[propagating] / nout], axis=-1)
        native_basis = np.stack([native_s, native_p], axis=-1)
        desired_basis = np.stack([desired_s, desired_p], axis=-1)
        dout = np.einsum('nva,nvb->nab', desired_basis, native_basis)
        matrix = np.stack([np.stack([gc[prefix+'ss'], gc[prefix+'ps']], axis=-1),
                           np.stack([gc[prefix+'sp'], gc[prefix+'pp']], axis=-1)], axis=-2)[propagating]
        power = dout @ matrix @ din
        electric = power / np.sqrt(q)[:, None, None]
        cartesian = np.stack([np.stack([fields[f'ek{axis}_{direction}{pol}'] for pol in 'sp'], axis=-1)
                              for axis in 'xyz'], axis=-2)[propagating]
        field_electric = np.einsum('nva,nvb->nab', desired_basis, cartesian) @ din
        error = float(np.max(np.abs(field_electric - electric)))
        require(error <= float(settings.get('conversion_absolute', 1e-8)),
                f'{prefix}: Cartesian field amplitudes disagree with native S conversion ({error:g}).')
        factor = registration_factor(selected, period, settings['mesh_cells_x'], registration)
        electric = electric * factor[:, None, None]
        power = power * factor[:, None, None]
        eta = np.abs(power) ** 2
        for j, pol in enumerate('sp'):
            # General RCWA: grating_orders = efficiency * n_inc*cos(theta_inc).
            # This adapter strictly requires theta=0 and incident air n=1,
            # so the factor is exactly one. Never reuse this comparison for
            # oblique incidence or another input medium without that factor.
            expected = np.asarray(go[prefix + pol + '_grating'])[propagating]
            require(np.allclose(np.sum(eta[:, :, j], axis=1), expected, atol=1e-8, rtol=1e-7),
                    f'{prefix}{pol}: S-channel powers disagree with grating orders.')
            output[f'eta_{side}_{pol}'] = np.sum(eta[:, :, j], axis=1)
            diagnostics['total_power'][prefix + pol] = float(np.sum(eta[:, :, j]))
            native_total = _total_energy(datasets['total_energy'], prefix+pol, model)
            require(abs(native_total - diagnostics['total_power'][prefix+pol]) <= 1e-7,
                    f'{prefix}{pol}: summed channel power disagrees with independent native total_energy.')
        output[f'{side}_orders'] = selected
        output[f'{side}_theta_deg'] = np.rad2deg(np.arcsin(kx[propagating] / nout))
        output[f'{side}_E'] = electric
        output[f'{side}_power'] = power
        diagnostics['native_to_cartesian_max_error'][prefix] = error
    for pol in 'sp':
        total = diagnostics['total_power']['R'+pol] + diagnostics['total_power']['T'+pol]
        if absorbing:
            require(-1e-7 <= total <= 1+1e-7, f'Passive RCWA R+T exceeds incident power for {pol}.')
            diagnostics.setdefault('unreturned_power', {})[pol] = 1.-total
        else:
            require(abs(total - 1.) <= 1e-7, f'Lossless RCWA energy balance fails for {pol}.')
    if resolved_spec is not None:
        diagnostics['transmission_scope'] = resolved_spec['transmission_scope']
        diagnostics['energy_check'] = ('Independent native S, Cartesian fields, diffraction powers and total flux; '
            'passivity bounds only. 1-R-T is a residual, not an independent absorption measurement.'
            if absorbing else 'Independent native channels and total flux; lossless R+T closure.')
    output['S11'] = output['r_power']
    output['S21'] = output['t_power']
    return output, diagnostics


def validate_material_index(dataset, resolved_spec):
    """Compare actual native complex index values without guessing packed axes."""
    expected = {complex(1.)}
    expected.update(complex(box['n'], box.get('k', 0.)) for box in resolved_spec['boxes'])
    expected = np.asarray(sorted(expected, key=lambda n: (n.real, n.imag)))
    metadata = dataset.get('Lumerical_dataset', {})
    names = metadata.get('attributes', [])
    # Actual v252 isotropic 2D acquisitions expose only index_z, packed by
    # layer intervals. Do not fabricate missing Cartesian components. Material
    # metadata plus independently queried x/y/z optical responses establish
    # isotropy; native index_z checks what the RCWA mesh actually used.
    scalar = names == ['index_z']
    require(scalar or (len(names) == 3 and set(names) == {'index_x', 'index_y', 'index_z'}),
            'Expected native scalar isotropic index_z or three Cartesian index components.')
    for role in resolved_spec['material_resolution']['roles'].values():
        if role.get('kind') == 'database':
            validation = role.get('validation', {})
            require(validation.get('status') == 'PASS'
                    and validation.get('cartesian_components_checked') == [1, 2, 3],
                    'Native scalar index requires independent three-axis material isotropy checks.')
    arrays = [np.asarray(dataset[name]).reshape(-1) for name in names]
    require(all(a.shape == arrays[0].shape and np.isfinite(a).all() for a in arrays),
            'Invalid actual complex material index data.')
    require(all(np.allclose(a, arrays[0], atol=1e-7, rtol=1e-7) for a in arrays[1:]),
            'Actual RCWA index is anisotropic; isotropic-only conversion refused.')
    observed = np.unique(arrays[0])
    distances = np.abs(observed[:, None]-expected[None, :])
    require(float(np.max(np.min(distances, axis=1))) <= 1e-6,
            'Actual RCWA index differs from the declared FDTD MCM fit; no material fallback allowed.')
    require(float(np.max(np.min(distances, axis=0))) <= 1e-6,
            'A requested material is absent from the actual RCWA index mesh.')
    return {'status': 'PASS', 'absolute_index_tolerance': 1e-6,
            'native_index_representation': ('scalar_isotropic_index_z' if scalar else 'three_cartesian_components'),
            'native_attributes': names,
            'isotropy_evidence': 'Independent material metadata and getindex/getfdtdindex components 1,2,3; missing native axes are never fabricated.',
            'actual_unique_indices': [{'n': float(n.real), 'k': float(n.imag)} for n in observed],
            'maximum_index_error': float(np.max(np.min(distances, axis=1)))}


def substrate_depth_convergence(first, second):
    """An actual doubled-depth solve must agree in complex air-port reflection."""
    left, ld = first
    right, rd = second
    require(np.array_equal(left['r_orders'], right['r_orders']), 'Depth checks have different reflected orders.')
    norm = max(float(np.linalg.norm(right['r_E'])), 1e-8)
    relative = float(np.linalg.norm(left['r_E']-right['r_E']) / norm)
    absolute = float(np.max(np.abs(left['r_E']-right['r_E'])))
    power = max(abs(ld['total_power']['R'+p]-rd['total_power']['R'+p]) for p in 'sp')
    return {'status': 'PASS' if relative <= 1e-4 and power <= 1e-5 else 'FAIL',
            'complex_relative_l2': relative, 'complex_absolute_max': absolute,
            'total_reflected_power_absolute': power,
            'limits': {'complex_relative_l2': 1e-4, 'total_reflected_power_absolute': 1e-5},
            'scope': 'All propagating air-port complex reflection, depth D versus 2D; terminal T is not FDTD internal flux.'}


def verify_actual_material_settings(state, spec, settings):
    expected = {box['name']: box.get('material_name', '<Object defined dielectric>') for box in spec['boxes']}
    require(state.get('actual_material_assignments') == expected, 'Actual material assignment record is incomplete or changed.')
    fitting = state.get('actual_material_fit_settings', {})
    require(all(fitting.get(key) == 1. for key in (
        'fit materials with multi-coefficient model', 'fit sampled materials', 'fit analytic materials',
        'override materials frequency range')), 'Actual RCWA material fit settings are incomplete or disabled.')
    for key, wanted in zip(('materials minimum wavelength', 'materials maximum wavelength'),
                           spec['fit_wavelength_range_nm']):
        require(np.isclose(fitting.get(key, 0), wanted*1e-9, rtol=1e-12, atol=1e-15),
                'Actual RCWA material fitting range differs from the requested FDTD range.')
    actual = state.get('actual_solver', {})
    mesh = np.asarray(actual.get('actual mesh cells x', []), dtype=float).reshape(-1)
    require(mesh.size == 1 and mesh[0] == settings['mesh_cells_x'], 'Actual material/depth mesh differs from requested settings.')
    positions = np.asarray(actual.get('interface actual positions', [])).reshape(-1)
    expected_positions = np.asarray(spec['interfaces_nm'])*1e-9
    require(positions.shape == expected_positions.shape
            and np.allclose(positions, expected_positions, rtol=1e-10, atol=1e-15),
            'Actual material/depth interfaces differ from explicit layer geometry.')


def read_acquisition(folder):
    folder = Path(folder).resolve()
    state = json.loads((folder / 'native_status.json').read_text(encoding='utf8'))
    require(state.get('status') == 'RAW_COMPLETE_PHASE_UNCALIBRATED', 'Native acquisition is incomplete or failed.')
    require(state.get('backend_fingerprint') == backend_fingerprint(), 'RCWA backend fingerprint changed; raw data must be reacquired.')
    frozen = json.loads((folder / 'input.json').read_text(encoding='utf8'))
    verify_material_databases(frozen['case']['model'])
    require(state.get('input_sha256') == canonical_hash(frozen), 'Frozen RCWA input checksum mismatch.')
    require(frozen.get('backend_fingerprint') == backend_fingerprint(), 'Frozen RCWA backend fingerprint is stale.')
    require(frozen.get('geometry') == geometry_spec(frozen['case']['model']), 'Frozen RCWA geometry differs from the shared geometry.')
    required_files = ['rcwa_model.fsp', 'native_rcwa.mat', 'build_run_export.lsf', 'worker.log'] + [
        f'native_{name}.{suffix}' for name in DATASETS for suffix in ('npz', 'json')]
    if has_named_materials(frozen['case']['model']):
        required_files += ['material_resolution.json', 'resolved_geometry.json', 'executed_build.lsf']
        if state.get('resolved_geometry', {}).get('absorbing_substrate'):
            required_files += ['depth_check/' + name for name in (
                ['rcwa_model.fsp', 'native_rcwa.mat', 'executed_build.lsf'] + [
                    f'native_{name}.{suffix}' for name in DATASETS for suffix in ('npz', 'json')])]
    require(isinstance(state.get('file_sha256'), dict), 'Native artifact checksums are absent.')
    for name in required_files:
        require((folder / name).is_file() and (folder / name).stat().st_size > 0, f'Missing or empty native artifact: {name}')
        require(state['file_sha256'].get(name) == sha256(folder / name), f'Native artifact checksum mismatch: {name}')
    require(str(state.get('lumerical_version', '')).startswith('8.34.'), 'Only installed 2025 R2 was scoped for this adapter.')
    datasets = {name: load_native_dataset(folder, name) for name in DATASETS}
    actual = state.get('actual_solver',{})
    mesh = np.asarray(actual.get('actual mesh cells x',[]),dtype=float).reshape(-1)
    require(mesh.size==1 and mesh[0]==frozen['settings']['mesh_cells_x'], 'Actual RCWA mesh differs from the calibrated requested mesh.')
    actual_spec = frozen['geometry']
    if has_named_materials(frozen['case']['model']):
        resolution = json.loads((folder/'material_resolution.json').read_text(encoding='utf8'))
        model = frozen['case']['model']
        require(resolution.get('material_databases') == model.get('material_databases', [])
                and resolution.get('fit_wavelength_range_nm') == list(fit_range_nm(model))
                and resolution.get('target_wavelength_nm') == float(model['wavelength_nm']),
                'Material resolution is not bound to frozen MDF content, wavelength and fitting range.')
        actual_spec = resolved_geometry(frozen['geometry'], resolution, frozen['settings'])
        require(state.get('material_resolution') == resolution, 'Material provenance differs from its archived resolution.')
        require(state.get('resolved_geometry') == actual_spec
                and json.loads((folder/'resolved_geometry.json').read_text(encoding='utf8')) == actual_spec,
                'Resolved RCWA geometry differs from frozen shapes and measured material response.')
        verify_actual_material_settings(state, actual_spec, frozen['settings'])
        require(state.get('material_index_verification') == validate_material_index(datasets['index'], actual_spec),
                'Actual material-index proof does not reproduce from raw index data.')
    positions = np.asarray(actual.get('interface actual positions',[]),dtype=float).reshape(-1)
    expected = np.asarray(actual_spec['interfaces_nm'])*1e-9
    require(positions.shape==expected.shape and np.allclose(positions,expected,rtol=1e-10,atol=1e-15),
            'Actual RCWA outer/internal interfaces differ from the frozen geometry.')
    axes,shape,attributes = _layout(datasets['grating_characterization'])
    require('n' in axes, 'Missing actual native order axis.')
    actual_count = np.asarray(actual.get('num_k',[]),dtype=float).reshape(-1)
    require(actual_count.size==1 and actual_count[0]==len(axes['n'][1]), 'Recorded actual num_k disagrees with the native full-order axis.')
    validate_native_transverse_origin(datasets['index'], frozen['case']['model'], frozen['settings'])
    if actual_spec.get('absorbing_substrate'):
        doubled = resolved_geometry(frozen['geometry'], state['material_resolution'], frozen['settings'], 2.)
        record = state.get('substrate_depth_check', {})
        require(record.get('resolved_geometry') == doubled, 'Missing actual doubled-depth geometry.')
        verify_actual_material_settings(record, doubled, frozen['settings'])
        require(all(record.get('material_resolution', {}).get(key) == state['material_resolution'][key]
                    for key in ('roles', 'material_databases', 'fit_wavelength_range_nm', 'target_wavelength_nm')),
                'Doubled-depth material response differs from the primary acquisition.')
        second = {name: load_native_dataset(folder/'depth_check', name) for name in DATASETS}
        check = substrate_depth_convergence(
            convert_native(datasets, frozen['case']['model'], frozen['settings'], resolved_spec=actual_spec),
            convert_native(second, frozen['case']['model'], frozen['settings'], resolved_spec=doubled))
        require(record.get('convergence') == check, 'Doubled-depth convergence does not reproduce from raw native files.')
        require(record.get('material_index_verification') == validate_material_index(second['index'], doubled),
                'Doubled-depth actual material-index proof changed.')
        validate_native_transverse_origin(second['index'], frozen['case']['model'], frozen['settings'])
    return frozen, state, datasets


def calibration_scope(model):
    return {'wavelength_nm': model['wavelength_nm'], 'period_nm': model['period_nm'],
            'n_substrate': model['n_substrate'], 'n_incident': 1.,
            'reference_y_um': model['sim']['reference_y_um'],
            'transmission_y_um': model['sim']['transmission_y_um'],
            'coordinates': 'normal incidence; real constant isotropic media; rectangular staircase; Q=(x,z,-y)'}


def acquisition_evidence(folder, frozen, state):
    """Bind a verdict to the whole current acquisition, not a saved PASS label."""
    return {'folder': str(Path(folder).resolve()), 'input_sha256': state['input_sha256'],
            'native_status_sha256': sha256(Path(folder) / 'native_status.json'),
            'backend_fingerprint': state['backend_fingerprint'],
            'file_sha256': dict(state['file_sha256']), 'settings': frozen['settings'],
            'lumerical_version': state['lumerical_version']}


def verify_absolute_origin(origin_folders, levels, scope, version):
    """Recompute independent absolute TE Born and chi-linearity controls.

    The evaluator's pure API reads no files and writes no diagnostic report.
    Importing it here avoids a module-initialization cycle with its CLI.
    """
    from tools import validate_rcwa_origin as origin
    folders = list(origin_folders or [])
    require(len(folders) == 4 and len({str(Path(p).resolve()) for p in folders}) == 4,
            'Absolute x=0 phase requires four distinct real weak Born acquisitions.')
    by_mesh = {level['mesh_cells_x']: level for level in levels}
    require(len(levels) == len(by_mesh) == 2, 'Absolute-origin controls require exactly two independently calibrated mesh levels.')
    require(tuple(origin.HYPOTHESES) == REGISTRATIONS, 'Origin evaluator and adapter hypotheses disagree.')
    records, evidence, seen, baseline = [], [], set(), None
    for folder in folders:
        frozen, state, datasets = read_acquisition(folder)
        model, settings = frozen['case']['model'], frozen['settings']
        mesh, index = settings['mesh_cells_x'], float(model['n_ridge'])
        require(mesh in by_mesh and settings == by_mesh[mesh], 'Born settings differ from the independently calibrated target levels.')
        require(state['lumerical_version'] == version, 'Born and target controls use different actual solver versions.')
        require(model.get('geometry_kind', 'ah53') == 'ah53'
                and model['n_substrate'] == 1 and model['ridge_height_nm'] == 100
                and model['ridge_cd_nm'] == 1600 and model['child_pitch_nm'] == 3200
                and not model.get('films') and model.get('ridge_shift_nm', 0) == 0
                and model.get('ridge_center_offsets_nm', [0, 0, 0]) == [0, 0, 0]
                and not model.get('ridge_top_left_inset_nm', 0)
                and not model.get('ridge_top_right_inset_nm', 0)
                and index in (1.00005, 1.0001),
                'Absolute-origin controls must be the predeclared symmetric 100nm weak ridges in air.')
        normalized = dict(model)
        normalized.pop('n_ridge')
        require(baseline is None or normalized == baseline, 'Born controls change more than the declared ridge susceptibility.')
        baseline = normalized
        weak_scope = calibration_scope(model)
        weak_scope['n_substrate'] = scope['n_substrate']
        require(weak_scope == scope, 'Born and target controls differ in period, wavelength or physical reference planes.')
        require(frozen.get('geometry') == geometry_spec(model), 'Born frozen geometry differs from the shared geometry.')
        require((mesh, index) not in seen, 'Duplicate Born mesh/contrast acquisition.')
        seen.add((mesh, index))
        arrays, diagnostic = convert_native(datasets, model, settings, 'native')
        # Keep the evaluator and export implementation of each declared
        # non-fitted coordinate conversion tied together explicitly.
        for hypothesis in REGISTRATIONS:
            require(np.allclose(registration_factor(origin.ORDERS, model['period_nm'], mesh, hypothesis),
                                origin.hypothesis_factor(model, settings, origin.ORDERS, hypothesis),
                                rtol=0, atol=1e-14), 'Independent origin and export phase factors disagree.')
        records.append((frozen, state, arrays))
        evidence.append(acquisition_evidence(folder, frozen, state))
    require(seen == {(mesh, index) for mesh in by_mesh for index in (1.00005, 1.0001)},
            'Each calibrated mesh requires both independent Born contrasts.')
    candidates = origin.evaluate_origin(records)
    winners = [name for name, result in candidates.items() if result['status'] == 'PASS']
    return {'status': 'PASS' if winners == ['cell_origin_half_cell'] else 'FAIL',
            'registration': winners[0] if len(winners) == 1 else None,
            'passing_hypotheses': winners, 'source_sha256': sha256(ORIGIN_TOOL),
            'formula': 'global a_m=native a_m*exp(-i*2*pi*m*(-period/2+dx/2)/period); dx=period/actual_mesh_cells_x',
            'scope': {'n_substrate': 1, 'ridge_height_nm': 100, 'ridge_cd_nm': 1600,
                      'child_pitch_nm': 3200, 'n_ridge': [1.00005, 1.0001],
                      'target_scope': scope, 'purpose': 'Independent absolute physical x=0 origin, no FDTD fitting'},
            'tolerances': dict(origin.TOLERANCES), 'orders': list(origin.ORDERS),
            'evidence': evidence, 'candidates': candidates}


def calibrate(acquisition_folders, registration='native', origin_folders=None):
    """Test a declared registration on independent controls; never fit phase data."""
    require(registration in REGISTRATIONS, 'Unknown registration hypothesis.')
    report = {'schema_version': '2.0', 'status': 'FAIL', 'backend_fingerprint': backend_fingerprint(),
              'registration': registration, 'correction_policy': 'No fitted phase; candidate fixed before validation; native files unchanged.',
              'absolute_phase_status': 'UNVERIFIED',
              'checks': [], 'evidence': [], 'levels': [], 'scope': None,
              'convergence_scope': 'Adjacent mesh/truncation levels: all r_E/t_E channels for |order|<=5; all-propagating-order total R/T powers.',
              'convergence_tolerances': {'complex_relative':0.03,'amplitude_floor':1e-4,'total_power_absolute':0.01}}
    records = []
    def check(name, error, limit):
        report['checks'].append({'name': name, 'error': float(error), 'limit': limit,
                                 'status': 'PASS' if math.isfinite(float(error)) and error <= limit else 'FAIL'})
    for folder in acquisition_folders:
        frozen, state, datasets = read_acquisition(folder)
        model, settings = frozen['case']['model'], frozen['settings']
        arrays, diagnostics = convert_native(datasets, model, settings, registration)
        scope = calibration_scope(model)
        if model.get('geometry_kind') != 'planar_interface':
            if report['scope'] is None:
                report['scope'] = scope
            require(report['scope'] == scope, 'AH53 calibration controls have inconsistent physical scope.')
        report['evidence'].append(acquisition_evidence(folder, frozen, state))
        records.append((frozen, arrays, diagnostics))
    require(report['scope'] is not None, 'Missing AH53 calibration scope.')
    for frozen, arrays, diagnostics in records:
        scope = calibration_scope(frozen['case']['model'])
        if frozen['case']['model'].get('geometry_kind') == 'planar_interface':
            scope['n_substrate'] = report['scope']['n_substrate']
        require(scope == report['scope'], 'Planar and AH53 controls must share wavelength, period and reference planes.')
    meshes = sorted({record[0]['settings']['mesh_cells_x'] for record in records})
    require(len(meshes) >= 2, 'Phase registration requires real independent acquisitions at at least two x meshes.')
    versions = {item['lumerical_version'] for item in report['evidence']}
    require(len(versions) == 1, 'Calibration must use one actual solver version.')
    report['lumerical_version'] = next(iter(versions))
    for mesh in meshes:
        level = [record for record in records if record[0]['settings']['mesh_cells_x'] == mesh]
        levels = {canonical_hash(record[0]['settings']) for record in level}
        require(len(levels) == 1, 'Each calibration mesh must have exactly one truncation/settings tuple.')
        report['levels'].append(level[0][0]['settings'])
        air = [record for record in level if record[0]['case']['model'].get('geometry_kind') == 'planar_interface' and record[0]['case']['model']['n_substrate'] == 1]
        glass = [record for record in level if record[0]['case']['model'].get('geometry_kind') == 'planar_interface' and record[0]['case']['model']['n_substrate'] == 1.5]
        require(len(glass) == 1 and len(air)<=1, 'Every calibration mesh requires its own real n=1.5 planar control; air is optional.')
        for label, group in (('air', air), ('n15', glass)):
            if not group:
                continue
            frozen, arrays, diagnostics = group[0]
            model = frozen['case']['model']
            expected = ((1-model['n_substrate'])/(1+model['n_substrate'])
                        * np.exp(4j*np.pi*1800./float(model['wavelength_nm'])))
            pos = int(np.flatnonzero(arrays['r_orders'] == 0)[0])
            check(f'{mesh}_{label}_complex_same_direction_p', np.max(np.abs(arrays['r_E'][pos] - np.eye(2)*expected)), 1e-8)
            check(f'{mesh}_{label}_parasitic_power', sum(np.sum(arrays[f'eta_r_{pol}'][arrays['r_orders'] != 0]) for pol in 'sp'), 1e-10)
            transmitted = (2/(1+model['n_substrate']) * np.exp(2j*np.pi*(1800.+model['n_substrate']*1050.)/float(model['wavelength_nm'])))
            tpos = int(np.flatnonzero(arrays['t_orders'] == 0)[0])
            check(f'{mesh}_{label}_transmission_complex', np.max(np.abs(arrays['t_E'][tpos]-np.eye(2)*transmitted)), 1e-8)
        base = [record for record in level if record[0]['case']['model'].get('geometry_kind', 'ah53') == 'ah53' and record[0]['case']['model'].get('ridge_shift_nm', 0) == 0]
        moved = [record for record in level if record[0]['case']['model'].get('geometry_kind', 'ah53') == 'ah53' and record[0]['case']['model'].get('ridge_shift_nm', 0) != 0]
        require(len(base) == len(moved) == 1, 'Every calibration mesh requires one symmetric zero-shift AH53 and one held-out rigid translation.')
        frozen, arrays, _ = base[0]
        model = frozen['case']['model']
        require(model.get('ridge_center_offsets_nm', [0,0,0]) == [0,0,0]
                and not model.get('films') and not model.get('ridge_top_left_inset_nm',0)
                and not model.get('ridge_top_right_inset_nm',0), 'Symmetry control must be a bare mirror-symmetric AH53.')
        other, translated, _ = moved[0]
        first = dict(model); second = dict(other['case']['model'])
        dx = float(second.pop('ridge_shift_nm', 0)) - float(first.pop('ridge_shift_nm', 0))
        require(first == second, 'Translation control changes more than ridge_shift_nm.')
        require(abs(dx) > 0 and np.array_equal(arrays['r_orders'], translated['r_orders']), 'Invalid rigid translation control.')
        for j, pol in enumerate('sp'):
            count = 0
            for order in (1, 5):
                positive = np.flatnonzero(arrays['r_orders'] == order)
                negative = np.flatnonzero(arrays['r_orders'] == -order)
                require(len(positive) == len(negative) == 1, 'Missing required symmetry orders.')
                ip, im = int(positive[0]), int(negative[0])
                a, b = arrays['r_E'][ip,j,j], arrays['r_E'][im,j,j]
                c, d = translated['r_E'][ip,j,j], translated['r_E'][im,j,j]
                require(min(abs(a),abs(b),abs(c),abs(d)) >= 1e-4,
                        'Calibration phase is unresolved below the amplitude floor; no certificate may be issued.')
                check(f'{mesh}_mirror_{pol}_m{order}', abs(a-b), 1e-7)
                for index, before, after in ((order,a,c),(-order,b,d)):
                    expected = np.exp(-2j*np.pi*index*dx/float(model['period_nm']))
                    check(f'{mesh}_translation_{pol}_m{index}', abs(after/before-expected), 0.02)
                count += 1
            check(f'{mesh}_observable_pairs_{pol}', 2-count, 0)
    require(report['scope'] is not None, 'Missing AH53 calibration scope.')
    require(report['scope']['n_substrate'] == 1.5, 'Initial calibrated scope requires n_substrate=1.5.')
    by_case = {}
    for record in records:
        by_case.setdefault(record[0]['case']['digest'],[]).append(record)
    for digest, group in by_case.items():
        group.sort(key=lambda record:record[0]['settings']['mesh_cells_x'])
        require(len(group)==len(meshes), 'Every calibration control must exist at all declared meshes.')
        for coarse,fine in zip(group,group[1:]):
            require(fine[0]['settings']['max_number_k_vectors']>coarse[0]['settings']['max_number_k_vectors'],
                    'The finer calibration mesh must independently increase Fourier truncation.')
            label=f'{digest}_{coarse[0]["settings"]["name"]}_{fine[0]["settings"]["name"]}'
            for side in ('r','t'):
                require(np.array_equal(coarse[1][side+'_orders'],fine[1][side+'_orders']), 'Convergence order sets differ.')
                mask=np.abs(fine[1][side+'_orders'])<=5
                denominator=np.maximum(np.abs(fine[1][side+'_E'][mask]),1e-4)
                error=np.max(np.abs(coarse[1][side+'_E'][mask]-fine[1][side+'_E'][mask])/denominator)
                check(f'{label}_{side}_complex_relative_convergence_m_le_5',error,0.03)
            total_error=max(abs(coarse[2]['total_power'][name]-fine[2]['total_power'][name]) for name in ('Rs','Rp','Ts','Tp'))
            check(f'{label}_total_power_absolute_convergence',total_error,0.01)
    report['absolute_origin'] = verify_absolute_origin(origin_folders, report['levels'], report['scope'],
                                                       report['lumerical_version'])
    absolute = (report['absolute_origin']['status'] == 'PASS'
                and registration == report['absolute_origin']['registration'] == 'cell_origin_half_cell')
    if absolute:
        report['absolute_phase_status'] = 'VERIFIED_ABSOLUTE_X0'
    report['status'] = 'PASS' if (absolute and report['checks']
                                and all(check['status']=='PASS' for check in report['checks'])) else 'FAIL'
    return report


def require_phase_calibration(certificate, model, settings, version):
    require(isinstance(certificate, dict) and certificate.get('status') == 'PASS',
            'RCWA phase is uncalibrated: a real multi-mesh calibration PASS certificate is required.')
    require(certificate.get('schema_version') == '2.0'
            and certificate.get('absolute_phase_status') == 'VERIFIED_ABSOLUTE_X0'
            and certificate.get('registration') == 'cell_origin_half_cell',
            'Absolute physical x=0 requires schema 2 independent Born evidence; native/half_cell relative controls do not calibrate absolute phase.')
    require(certificate.get('backend_fingerprint') == backend_fingerprint(), 'Calibration backend fingerprint is stale.')
    requested_scope = calibration_scope(model)
    if model.get('geometry_kind') == 'planar_interface' and model['n_substrate']==1:
        requested_scope['n_substrate'] = certificate.get('scope',{}).get('n_substrate')
    require(certificate.get('scope') == requested_scope, 'Requested model lies outside the calibrated physical scope.')
    require(certificate.get('lumerical_version') == version, 'Calibration solver version mismatch.')
    require(settings in certificate.get('levels', []), 'Requested RCWA mesh/truncation is not independently calibrated.')
    require(certificate.get('registration') in REGISTRATIONS, 'Invalid calibrated registration.')
    if certificate['registration'] == 'cell_origin_half_cell':
        dx = float(model['period_nm']) / settings['mesh_cells_x']
        for box in geometry_spec(model)['boxes']:
            if box['material'] != 'substrate':
                require(all(abs(edge/dx-round(edge/dx)) < 1e-8 for edge in (box['x_min_nm'],box['x_max_nm'])),
                        'Absolute-origin calibration covers aligned rectangular boundaries only; requested geometry is off-grid.')
    evidence = certificate.get('evidence', [])
    require(len(evidence) >= 6, 'Calibration lacks the required real two-mesh control evidence.')
    origin_evidence = certificate.get('absolute_origin', {}).get('evidence', [])
    require(len(origin_evidence) == 4, 'Calibration lacks the four real absolute-origin Born controls.')
    for item in evidence + origin_evidence:
        folder = Path(item['folder'])
        require(sha256(folder/'native_status.json') == item.get('native_status_sha256'), 'Calibration evidence status checksum changed.')
        frozen, state, datasets = read_acquisition(folder)
        require(state['input_sha256'] == item.get('input_sha256'), 'Calibration input evidence checksum changed.')
    # Recalculate the checks from original acquisitions; editing a PASS label cannot open this gate.
    refreshed = calibrate([item['folder'] for item in evidence], certificate['registration'],
                          [item['folder'] for item in origin_evidence])
    require(refreshed['status'] == 'PASS' and canonical_hash(refreshed)==canonical_hash(certificate),
            'Calibration evidence no longer reproduces its PASS verdict.')


def export_calibrated(folder, certificate):
    frozen, state, datasets = read_acquisition(folder)
    model, settings = frozen['case']['model'], frozen['settings']
    if has_named_materials(model):
        return export_material_calibrated(folder, certificate, (frozen, state, datasets))
    require_phase_calibration(certificate, model, settings, state['lumerical_version'])
    arrays, diagnostic = convert_native(datasets, model, settings, certificate['registration'])
    folder = Path(folder)
    destination = folder / 'rcwa_coefficients.npz'
    np.savez_compressed(destination, **arrays)
    metadata = {'backend': 'RCWA', 'backend_fingerprint': backend_fingerprint(),
                'input_sha256': state['input_sha256'], 'lumerical_version': state['lumerical_version'],
                'phase_status': 'CALIBRATED_ON_CURRENT_RCWA_CONTROL_FAMILY',
                'absolute_phase_status': certificate['absolute_phase_status'],
                'absolute_origin_proof_sha256': canonical_hash(certificate['absolute_origin']),
                'scope': certificate['scope'], 's_matrix_scope': diagnostic['s_matrix_scope'],
                'registration': certificate['registration'], 'materials': frozen['geometry']['source_shapes_xy_nm'],
                'reference_planes': {key: frozen['geometry'][key] for key in (
                    'source_reference_z_nm','reflection_reference_z_nm','transmission_reference_z_nm')},
                'basis': 'AH53 s=+Z, input p=+X; outgoing p has positive X component for both sides',
                'power': 'S11=r_power=sqrt(q_air_out/q_in)*r_E; S21=t_power=sqrt(q_sub_out/q_in)*t_E',
                'phase_convention': 'exp(+ik.r-iwt); fixed physical x=0 registered only by independently validated hypothesis',
                'certificate_sha256': canonical_hash(certificate), 'result_sha256': sha256(destination),
                'conversion_diagnostics': diagnostic}
    write_json(folder/'rcwa_coefficients.metadata.json', metadata)
    return destination


def _material_family(model):
    family = copy.deepcopy(model)
    family.pop('ridge_shift_nm', None)
    return canonical_hash(family)


def calibrate_materials(acquisition_folders, origin_folders=None):
    """A separate evidence gate for the exact named-material acquisitions.

    Every active named material needs a complex Fresnel planar control at each
    requested mesh. Diffracting families additionally need real held-out rigid
    translations, two-mesh convergence and the independent absolute Born proof.
    This certificate never extends the legacy lossless n=1.5 calibration.
    """
    report = {'schema_version': 'materials-1.0', 'status': 'FAIL',
              'backend_fingerprint': backend_fingerprint(), 'registration': 'cell_origin_half_cell',
              'checks': [], 'evidence': [], 'levels': [],
              'absolute_phase_status': 'UNVERIFIED',
              'scope': {'mode': 'exact_acquisitions_only', 'incident_medium': 'air',
                        'materials': 'passive isotropic named MCM fit', 'acquisition_inputs': []},
              'tolerances': {'planar_complex_absolute': 1e-6, 'parasitic_power': 1e-10,
                             'translation_complex_relative': .02, 'mesh_complex_l2': .03,
                             'mesh_total_R_absolute': .01}}
    def check(name, error, limit):
        error = float(error)
        report['checks'].append({'name': name, 'error': error, 'limit': limit,
                                'status': 'PASS' if math.isfinite(error) and error <= limit else 'FAIL'})
    records, versions, scopes = [], set(), set()
    for folder in acquisition_folders:
        frozen, state, datasets = read_acquisition(folder)
        model, settings = frozen['case']['model'], frozen['settings']
        require(has_named_materials(model), 'Material calibration only accepts named-material acquisitions.')
        actual = state['resolved_geometry']
        arrays, diagnostic = convert_native(datasets, model, settings, report['registration'], actual)
        records.append((frozen, state, arrays, diagnostic))
        report['evidence'].append(acquisition_evidence(folder, frozen, state))
        report['scope']['acquisition_inputs'].append(state['input_sha256'])
        versions.add(state['lumerical_version'])
        scopes.add(canonical_hash({key: model[key] for key in ('wavelength_nm', 'period_nm')} | {
            'fit_range': list(fit_range_nm(model)), 'material_databases': model.get('material_databases', []),
            'reference_y_um': model['sim']['reference_y_um'],
            'transmission_y_um': model['sim']['transmission_y_um']}))
        if actual['absorbing_substrate']:
            convergence = state['substrate_depth_check']['convergence']
            check(f'{frozen["case"]["id"]}_{settings["name"]}_substrate_depth_complex',
                  convergence['complex_relative_l2'], 1e-4)
            check(f'{frozen["case"]["id"]}_{settings["name"]}_substrate_depth_R',
                  convergence['total_reflected_power_absolute'], 1e-5)
    require(records and len(versions) == len(scopes) == 1,
            'Material controls must share one solver version, wavelength, period, fit range, MDF and reference planes.')
    report['lumerical_version'] = next(iter(versions))
    meshes = sorted({r[0]['settings']['mesh_cells_x'] for r in records})
    diffracting = [r for r in records if r[0]['case']['model'].get('geometry_kind', 'ah53') != 'planar_interface']
    if diffracting:
        require(len(meshes) == 2, 'Diffracting named-material controls require exactly two independent meshes.')
    planar_materials = {}
    for mesh in meshes:
        level = [r for r in records if r[0]['settings']['mesh_cells_x'] == mesh]
        require(len({canonical_hash(r[0]['settings']) for r in level}) == 1,
                'One complete settings tuple is required per material-control mesh.')
        report['levels'].append(level[0][0]['settings'])
        for frozen, state, arrays, diagnostic in level:
            model = frozen['case']['model']
            if model.get('geometry_kind') != 'planar_interface':
                continue
            require(not model.get('films'), 'Material planar controls must have no films.')
            sub = state['material_resolution']['roles']['substrate']
            n = complex(sub['n'], sub['k'])
            expected = (1-n)/(1+n) * np.exp(4j*np.pi*1800./float(model['wavelength_nm']))
            i = int(np.flatnonzero(arrays['r_orders'] == 0)[0])
            label = f'{frozen["case"]["id"]}_{mesh}'
            check(label+'_complex_Fresnel', np.max(np.abs(arrays['r_E'][i]-np.eye(2)*expected)), 1e-6)
            check(label+'_parasitic_reflection', sum(np.sum(arrays['eta_r_'+p][arrays['r_orders'] != 0]) for p in 'sp'), 1e-10)
            if sub.get('kind') == 'database':
                planar_materials[(mesh, sub['name'])] = sub
        for frozen, state, arrays, diagnostic in level:
            for box in state['resolved_geometry']['boxes']:
                if not box.get('material_name'):
                    continue
                name = box['material_name']
                control = planar_materials.get((mesh, name))
                require(control is not None, f'Missing independent planar material control for {name!r} at mesh {mesh}.')
                require(abs(complex(control['n'], control['k'])-complex(box['n'], box['k'])) <= 1e-12,
                        'Planar and structured material models differ.')
        groups = {}
        for record in level:
            if record[0]['case']['model'].get('geometry_kind', 'ah53') != 'planar_interface':
                groups.setdefault(_material_family(record[0]['case']['model']), []).append(record)
        for family, group in groups.items():
            zero = [r for r in group if float(r[0]['case']['model'].get('ridge_shift_nm', 0)) == 0]
            moved = [r for r in group if float(r[0]['case']['model'].get('ridge_shift_nm', 0)) != 0]
            require(len(zero) == 1 and bool(moved), 'Every material geometry family needs its own zero-shift and held-out translation.')
            base = zero[0]
            model = base[0]['case']['model']
            for translated in moved:
                shift = float(translated[0]['case']['model']['ridge_shift_nm'])
                a, b = base[2]['r_E'], translated[2]['r_E']
                orders = base[2]['r_orders']
                require(np.array_equal(orders, translated[2]['r_orders']), 'Material translation changes air-port order coverage.')
                expected = np.exp(-2j*np.pi*orders*shift/float(model['period_nm']))
                for j, pol in enumerate('sp'):
                    mask = (np.abs(orders) <= 5) & (orders != 0) & (np.abs(a[:, j, j]) >= 1e-4)
                    require(np.count_nonzero(mask) >= 2, 'Material translation phase has fewer than two observable nonzero orders.')
                    error = np.max(np.abs(b[mask, j, j]/a[mask, j, j]-expected[mask]))
                    check(f'{family}_{mesh}_{shift}_translation_{pol}', error, .02)
    if diffracting:
        levels = report['levels']
        require(levels[1]['max_number_k_vectors'] > levels[0]['max_number_k_vectors'],
                'Finer material controls must independently increase Fourier truncation.')
        baseline = records[0][0]['case']['model']
        scope = {'wavelength_nm': baseline['wavelength_nm'], 'period_nm': baseline['period_nm'],
                 'n_substrate': 1.5, 'n_incident': 1.,
                 'reference_y_um': baseline['sim']['reference_y_um'],
                 'transmission_y_um': baseline['sim']['transmission_y_um'],
                 'coordinates': 'normal incidence; real constant isotropic media; rectangular staircase; Q=(x,z,-y)'}
        proof = verify_absolute_origin(origin_folders, levels, scope, report['lumerical_version'])
        report['absolute_origin'] = proof
        check('independent_absolute_x0_origin', 0 if proof['status'] == 'PASS' else 1, 0)
        report['absolute_phase_status'] = 'VERIFIED_ABSOLUTE_X0' if proof['status'] == 'PASS' else 'UNVERIFIED'
        by_case = {}
        for record in records:
            by_case.setdefault(record[0]['case']['digest'], []).append(record)
        for digest, group in by_case.items():
            require(len(group) == 2, 'Each material control must exist at both mesh levels.')
            coarse, fine = sorted(group, key=lambda r: r[0]['settings']['mesh_cells_x'])
            require(np.array_equal(coarse[2]['r_orders'], fine[2]['r_orders']), 'Material mesh order sets differ.')
            mask = np.abs(fine[2]['r_orders']) <= 5
            norm = max(float(np.linalg.norm(fine[2]['r_E'][mask])), 1e-8)
            check(digest+'_mesh_reflection_complex_l2',
                  np.linalg.norm(coarse[2]['r_E'][mask]-fine[2]['r_E'][mask])/norm, .03)
            check(digest+'_mesh_reflection_total_power',
                  max(abs(coarse[3]['total_power']['R'+p]-fine[3]['total_power']['R'+p]) for p in 'sp'), .01)
        for frozen, state, arrays, diagnostic in records:
            dx = frozen['case']['model']['period_nm']/frozen['settings']['mesh_cells_x']
            require(all(abs(box[edge]/dx-round(box[edge]/dx)) < 1e-8
                        for box in frozen['geometry']['boxes'] if box['material'] != 'substrate'
                        for edge in ('x_min_nm', 'x_max_nm')),
                    'Material absolute-origin scope requires aligned rectangular x boundaries.')
    else:
        report['absolute_phase_status'] = 'VERIFIED_PLANAR_SPECULAR_ONLY'
        report['absolute_origin'] = None
    report['status'] = 'PASS' if report['checks'] and all(c['status'] == 'PASS' for c in report['checks']) else 'FAIL'
    return report


def require_material_calibration(certificate, folder):
    require(isinstance(certificate, dict) and certificate.get('schema_version') == 'materials-1.0'
            and certificate.get('status') == 'PASS', 'Named materials require their separate material-control certificate.')
    require(certificate.get('backend_fingerprint') == backend_fingerprint(), 'Material certificate fingerprint is stale.')
    evidence = certificate.get('evidence', [])
    require(evidence and str(Path(folder).resolve()) in {e['folder'] for e in evidence},
            'Material certificate covers only its exact real acquisitions, not arbitrary models.')
    for item in evidence + (certificate.get('absolute_origin') or {}).get('evidence', []):
        require(sha256(Path(item['folder'])/'native_status.json') == item['native_status_sha256'],
                'Material calibration evidence status checksum changed.')
    refreshed = calibrate_materials([e['folder'] for e in evidence],
        [e['folder'] for e in (certificate.get('absolute_origin') or {}).get('evidence', [])])
    require(refreshed['status'] == 'PASS' and canonical_hash(refreshed) == canonical_hash(certificate),
            'Material certificate does not reproduce from actual native evidence.')


def export_material_calibrated(folder, certificate, acquisition=None):
    frozen, state, datasets = acquisition or read_acquisition(folder)
    require_material_calibration(certificate, folder)
    actual = state['resolved_geometry']
    arrays, diagnostic = convert_native(datasets, frozen['case']['model'], frozen['settings'],
                                         certificate['registration'], actual)
    folder = Path(folder)
    destination = folder/'rcwa_coefficients.npz'
    np.savez_compressed(destination, **arrays)
    metadata = {'backend': 'RCWA', 'backend_fingerprint': backend_fingerprint(),
        'input_sha256': state['input_sha256'], 'lumerical_version': state['lumerical_version'],
        'phase_status': 'CALIBRATED_ON_EXACT_MATERIAL_CONTROL_ACQUISITIONS',
        'absolute_phase_status': certificate['absolute_phase_status'],
        'scope': certificate['scope'], 'registration': certificate['registration'],
        's_matrix_scope': diagnostic['s_matrix_scope'],
        'materials': frozen['geometry']['source_shapes_xy_nm'],
        'material_resolution': state['material_resolution'],
        'material_index_verification': state['material_index_verification'],
        'substrate_depth_check': state.get('substrate_depth_check'),
        'terminal_negligible_fit_loss_approximation': actual['terminal_negligible_fit_loss_approximation'],
        'transmission_scope': actual['transmission_scope'],
        'transmission_validation': 'Terminal-port conversion independently matches native fields/powers; no claim of internal FDTD T equality.',
        'reference_planes': {key: actual[key] for key in ('source_reference_z_nm',
            'reflection_reference_z_nm', 'transmission_reference_z_nm')},
        'basis': 'AH53 s=+Z, input p=+X; outgoing p has positive X component for both sides',
        'power': 'S11=r_power=sqrt(q_air_out)*r_E; S21=t_power=sqrt(q_terminal_out)*t_E',
        'phase_convention': 'exp(+ik.r-iwt); fixed physical x=0',
        'certificate_sha256': canonical_hash(certificate), 'result_sha256': sha256(destination),
        'conversion_diagnostics': diagnostic}
    write_json(folder/'rcwa_coefficients.metadata.json', metadata)
    return destination
