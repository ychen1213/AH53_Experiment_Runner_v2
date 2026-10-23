"""Offline RCWA mathematics and closed gates; never writes solver results.

The arrays below are explicitly synthetic, in-memory algebra fixtures. They
do not represent an executed Lumerical calculation or physical validation.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from ah53runner import rcwa
from ah53runner.geometry import build_geometry


ROOT = Path(__file__).resolve().parents[1]


def model_fixture():
    model = json.loads((ROOT / 'configs' / 'quick_check.json').read_text(encoding='utf8'))['base_model']
    model['films'] = []
    return model


def settings_fixture():
    return {'name': 'synthetic_math_only', 'mesh_cells_x': 800,
            'max_number_k_vectors': 80, 'conversion_absolute': 1e-10}


def synthetic_datasets(model, planar_fresnel=False):
    """Construct mutually consistent S/Cartesian/power arrays only in memory."""
    orders = np.arange(-80, 81)
    shape = (1, 1, len(orders), 1)
    kx = orders * model['wavelength_nm'] / model['period_nm']
    matrices = {side: np.zeros((len(orders), 2, 2), dtype=complex) for side in 'RT'}
    for order in (-5, -1, 0, 1, 5):
        i = int(np.flatnonzero(orders == order)[0])
        # Distinct cross channels expose input/output transpositions.
        matrices['R'][i] = (1 + order / 20) * np.array(
            [[.015 + .01j, .005 - .003j], [.002 + .006j, .012 - .007j]])
        if order:
            matrices['T'][i] = (1 + order / 30) * np.array(
                [[.02 - .01j, .004 + .002j], [.003 - .005j, .017 + .008j]])
    i0 = int(np.flatnonzero(orders == 0)[0])
    for j in range(2):
        partial = sum(np.sum(np.abs(matrix[:, :, j]) ** 2) for matrix in matrices.values())
        matrices['T'][i0, j, j] = np.sqrt(1 - partial) * np.exp(.2j * (j + 1))
    if planar_fresnel:
        matrices['R'][:] = 0
        matrices['T'][:] = 0
        nsub = model['n_substrate']
        phase_r = np.exp(4j * np.pi * 1800 / model['wavelength_nm'])
        phase_t = np.exp(2j * np.pi * (1800 + nsub * 1050) / model['wavelength_nm'])
        r = (1 - nsub) / (1 + nsub)
        matrices['R'][i0] = np.diag([r, -r]) * phase_r
        matrices['T'][i0] = np.eye(2) * (2 * np.sqrt(nsub) / (1 + nsub)) * phase_t

    def dataset(attributes):
        result = {'lambda': np.array([[model['wavelength_nm'] * 1e-9]]),
                  'theta': np.array([[0.]]), 'phi': np.array([[0.]]),
                  'n': orders[:, None].astype(float), 'm': np.array([[0.]])}
        result['Lumerical_dataset'] = {
            'parameters': [['lambda'], ['theta', 'phi'], ['n'], ['m']],
            'attributes': list(attributes)}
        result.update({name: np.asarray(array).reshape(shape) for name, array in attributes.items()})
        return result

    gc, go, fields = {}, {'kx': kx, 'ky': np.zeros(len(orders))}, {}
    for prefix, nout, direction, kz_sign in (
            ('R', 1., 'b', -1), ('T', model['n_substrate'], 'f', 1)):
        matrix = matrices[prefix]
        for input_index, ipol in enumerate('sp'):
            for output_index, opol in enumerate('sp'):
                gc[prefix + ipol + opol] = matrix[:, output_index, input_index]
            go[prefix + ipol + '_grating'] = np.sum(np.abs(matrix[:, :, input_index]) ** 2, axis=1)
        for pol in 'sp':
            for axis in 'xyz':
                fields[f'ek{axis}_{direction}{pol}'] = np.zeros(len(orders), dtype=complex)
        for i, order in enumerate(orders):
            q2 = nout ** 2 - kx[i] ** 2
            if q2 <= 1e-12:
                continue
            q = np.sqrt(q2)
            a = 1. if order < 0 else -1.
            # Direct component formulas, independent of the adapter's cross
            # products and matrix projection implementation.
            native_s = np.array([0., a, 0.])
            native_p = np.array([-kz_sign * a * q / nout, 0., a * kx[i] / nout])
            for j, pol in enumerate('sp'):
                electric = (native_s * matrix[i, 0, j] + native_p * matrix[i, 1, j]) / np.sqrt(q)
                for axis, value in zip('xyz', electric):
                    fields[f'ek{axis}_{direction}{pol}'][i] = value
    total = {'lambda': np.array([[model['wavelength_nm'] * 1e-9]]),
             'theta': np.array([[0.]]), 'phi': np.array([[0.]]),
             'Lumerical_dataset': {'parameters': [['lambda'], ['theta', 'phi']],
                                  'attributes': ['Rs', 'Rp', 'Ts', 'Tp']}}
    for prefix in 'RT':
        for j, pol in enumerate('sp'):
            total[prefix + pol] = np.array([[np.sum(np.abs(matrices[prefix][:, :, j]) ** 2)]])
    return ({'grating_characterization': dataset(gc), 'grating_orders': dataset(go),
             'field_amplitudes': dataset(fields), 'total_energy': total}, orders, matrices)


class TestRCWAGeometry(unittest.TestCase):
    def setUp(self):
        self.model = model_fixture()
        self.settings = settings_fixture()

    def test_rotation_is_proper_and_maps_incidence_and_s(self):
        spec = rcwa.geometry_spec(self.model)
        rotation = np.asarray(spec['rotation_original_to_rcwa'])
        np.testing.assert_array_equal(rotation @ rotation.T, np.eye(3))
        self.assertAlmostEqual(np.linalg.det(rotation), 1.)
        np.testing.assert_array_equal(rotation @ [0, -1, 0], [0, 0, 1])
        np.testing.assert_array_equal(rotation @ [0, 0, 1], [0, 1, 0])

    def test_rotated_boxes_preserve_every_shared_shape_and_index(self):
        self.model['films'] = [{'mode': 'conformal', 'thickness_nm': 70, 'n': 1.46, 'k': 0}]
        shapes, _, _ = build_geometry(self.model)
        spec = rcwa.geometry_spec(self.model)
        self.assertEqual(len(spec['boxes']), len(shapes))
        for shape, box in zip(shapes, spec['boxes']):
            self.assertEqual((box['name'], box['material'], box['n']),
                             (shape.name, shape.material, shape.index_n))
            self.assertEqual((box['x_min_nm'], box['x_max_nm']), (shape.x1_nm, shape.x2_nm))
            self.assertEqual((box['z_min_nm'], box['z_max_nm']), (-shape.y2_nm, -shape.y1_nm))

    def test_outer_interfaces_remain_fixed_across_film_thicknesses(self):
        for thickness in (0, 200, 400):
            self.model['films'] = [{'mode': 'planarized', 'thickness_nm': thickness, 'n': 1.46}]
            spec = rcwa.geometry_spec(self.model)
            self.assertEqual(spec['interfaces_nm'][0], -1800.)
            self.assertEqual(spec['interfaces_nm'][-1], 1050.)
            self.assertNotIn(1600., spec['interfaces_nm'])
            self.assertEqual(spec['source_reference_z_nm'], -1800.)
            self.assertEqual(spec['reflection_reference_z_nm'], -1800.)
            self.assertEqual(spec['transmission_reference_z_nm'], 1050.)

    def test_slopes_loss_and_nonfinite_geometry_are_rejected(self):
        changes = [({'ridge_top_left_inset_nm': 30}),
                   ({'ridge_top_right_inset_nm': 30}),
                   ({'n_ridge': float('nan')}),
                   ({'films': [{'mode': 'conformal', 'thickness_nm': 20, 'n': 1.4, 'k': .01}]}),
                   ({'films': [{'mode': 'conformal', 'thickness_nm': 20, 'n': 1.4, 'k': -.01}]})]
        for change in changes:
            with self.subTest(change=change):
                model = copy.deepcopy(self.model)
                model.update(change)
                with self.assertRaises(ValueError):
                    rcwa.geometry_spec(model)

    def test_changed_reference_planes_are_rejected(self):
        for key, value in (('reference_y_um', 1.7), ('transmission_y_um', -1.0),
                           ('y_min_um', -1.0), ('y_max_um', 1.7)):
            with self.subTest(key=key):
                model = copy.deepcopy(self.model)
                model['sim'][key] = value
                with self.assertRaises(ValueError):
                    rcwa.geometry_spec(model)

    def test_settings_require_complete_propagating_orders_and_sampling(self):
        rcwa.validate_settings(self.settings, self.model)
        changes = [{'max_number_k_vectors': 37}, {'max_number_k_vectors': 76},
                   {'mesh_cells_x': 160}, {'mesh_cells_x': True}, {'max_number_k_vectors': 80.},
                   {'conversion_absolute': 0}, {'conversion_absolute': float('nan')},
                   {'conversion_absolute': 1e-3}, {'unrecognized_option': 1}]
        for change in changes:
            with self.subTest(change=change):
                settings = dict(self.settings, **change)
                with self.assertRaises(ValueError):
                    rcwa.validate_settings(settings, self.model)

    def test_lsf_uses_shared_fixed_interfaces_and_explicit_native_exports(self):
        spec = rcwa.geometry_spec(self.model)
        script = rcwa.build_lsf(spec, self.settings)
        self.assertEqual(script.count('addrect;'), len(spec['boxes']))
        for text in ('addrcwa;', '"2D Y-normal"', '"propagation axis","z"',
                     '"propagation direction","forward"', '"mesh refinement","staircase"',
                     '"use li factorization",0', '"interface absolute positions"'):
            self.assertIn(text, script)
        for name in rcwa.DATASETS:
            self.assertIn(f'{name}=getresult("RCWA","{name}");', script)


class TestRCWAMath(unittest.TestCase):
    def setUp(self):
        self.model = model_fixture()
        self.settings = settings_fixture()
        self.datasets, self.orders, self.matrices = synthetic_datasets(self.model)

    def test_registration_is_declared_signed_phase_without_power_change(self):
        orders = np.array([-10, -1, 0, 1, 10])
        native = rcwa.registration_factor(orders, 16000, 800, 'native')
        corrected = rcwa.registration_factor(orders, 16000, 800, 'half_cell')
        absolute = rcwa.registration_factor(orders, 16000, 800, 'cell_origin_half_cell')
        np.testing.assert_array_equal(native, np.ones(len(orders)))
        np.testing.assert_allclose(corrected, np.exp(-2j * np.pi * orders * 10 / 16000), atol=1e-15)
        np.testing.assert_allclose(np.abs(corrected), 1, atol=1e-15)
        np.testing.assert_allclose(absolute, corrected*np.exp(1j*np.pi*orders), atol=1e-14)
        np.testing.assert_allclose(np.abs(absolute), 1, atol=1e-15)
        self.assertLess(np.angle(corrected[-1]), 0)
        with self.assertRaises(ValueError):
            rcwa.registration_factor(orders, 16000, 800, 'fitted_phase')

    def test_absolute_registration_requires_observed_native_cell_centers(self):
        x = (-8000+(np.arange(800)+.5)*20)*1e-9
        dataset = {'Lumerical_dataset': {'geometry': 'rectilinear'}, 'x': x[:, None]}
        summary = rcwa.validate_native_transverse_origin(dataset, self.model, self.settings)
        self.assertAlmostEqual(summary['first_x_nm'], -7990)
        self.assertAlmostEqual(summary['last_x_nm'], 7990)
        original = dataset['x'].copy()
        for altered in (x+10e-9, x[::-1], x[:-1]):
            with self.subTest(shape=altered.shape), self.assertRaises(ValueError):
                rcwa.validate_native_transverse_origin(dict(dataset, x=altered), self.model, self.settings)
        np.testing.assert_array_equal(dataset['x'], original)

    def test_analytic_planar_fixture_has_same_direction_p_and_fixed_reference_phase(self):
        self.model['geometry_kind'] = 'planar_interface'
        datasets, _, _ = synthetic_datasets(self.model, planar_fresnel=True)
        arrays, diagnostics = rcwa.convert_native(datasets, self.model, self.settings)
        i0 = int(np.flatnonzero(arrays['r_orders'] == 0)[0])
        expected_r = .162717428756567 + .116288599522273j
        expected_t = .8 * np.exp(2j * np.pi * (1800 + 1.5 * 1050) / 643)
        np.testing.assert_allclose(arrays['r_E'][i0], np.eye(2) * expected_r, atol=2e-15)
        it = int(np.flatnonzero(arrays['t_orders'] == 0)[0])
        np.testing.assert_allclose(arrays['t_E'][it], np.eye(2) * expected_t, atol=2e-15)
        for pol in 'sp':
            self.assertAlmostEqual(diagnostics['total_power']['R' + pol], .04)
            self.assertAlmostEqual(diagnostics['total_power']['T' + pol], .96)
        self.assertEqual(diagnostics['phase_status'], 'UNCALIBRATED_DIAGNOSTIC_ONLY')

    def test_signed_order_basis_cross_channels_and_flux_normalization(self):
        arrays, diagnostics = rcwa.convert_native(self.datasets, self.model, self.settings)
        self.assertEqual(diagnostics['phase_status'], 'UNCALIBRATED_DIAGNOSTIC_ONLY')
        self.assertEqual(arrays['r_E'].shape, (49, 2, 2))
        self.assertEqual(arrays['t_E'].shape, (75, 2, 2))
        for prefix, side, nout in (('R', 'r', 1.), ('T', 't', 1.5)):
            for order in (-5, -1, 0, 1, 5):
                source = int(np.flatnonzero(self.orders == order)[0])
                target = int(np.flatnonzero(arrays[f'{side}_orders'] == order)[0])
                m = self.matrices[prefix][source]
                a = 1 if order < 0 else -1
                if prefix == 'R':
                    expected = a * np.array([[-m[0, 0], m[0, 1]], [-m[1, 0], m[1, 1]]])
                else:
                    expected = a * np.array([[-m[0, 0], m[0, 1]], [m[1, 0], -m[1, 1]]])
                kx = order * 643 / 16000
                q = np.sqrt(nout ** 2 - kx ** 2)
                with self.subTest(side=side, order=order):
                    np.testing.assert_allclose(arrays[f'{side}_power'][target], expected, atol=1e-14)
                    np.testing.assert_allclose(arrays[f'{side}_E'][target], expected / np.sqrt(q), atol=1e-14)
            for j, pol in enumerate('sp'):
                np.testing.assert_allclose(arrays[f'eta_{side}_{pol}'],
                                           np.sum(np.abs(arrays[f'{side}_power'][:, :, j]) ** 2, axis=1))
        np.testing.assert_array_equal(arrays['S11'], arrays['r_power'])
        np.testing.assert_array_equal(arrays['S21'], arrays['t_power'])
        for pol in 'sp':
            self.assertAlmostEqual(sum(diagnostics['total_power'][side + pol] for side in 'RT'), 1.)

    def test_native_arrays_are_not_modified_and_registration_is_only_a_hypothesis(self):
        originals = copy.deepcopy(self.datasets)
        raw, _ = rcwa.convert_native(self.datasets, self.model, self.settings)
        moved, diagnostic = rcwa.convert_native(self.datasets, self.model, self.settings, 'half_cell')
        for name in self.datasets:
            for key, value in self.datasets[name].items():
                if isinstance(value, np.ndarray):
                    np.testing.assert_array_equal(value, originals[name][key])
        self.assertEqual(diagnostic['phase_status'], 'UNCALIBRATED_DIAGNOSTIC_ONLY')
        for side in 'rt':
            factor = np.exp(-2j * np.pi * raw[f'{side}_orders'] / (2 * 800))
            np.testing.assert_allclose(moved[f'{side}_E'], raw[f'{side}_E'] * factor[:, None, None])
            np.testing.assert_allclose(np.abs(moved[f'{side}_power']), np.abs(raw[f'{side}_power']))

    def test_dataset_parameter_axis_order_is_read_from_metadata(self):
        expected, _ = rcwa.convert_native(self.datasets, self.model, self.settings)
        for dataset in self.datasets.values():
            metadata = dataset['Lumerical_dataset']
            groups = metadata['parameters']
            permutation = (2, 3, 0, 1) if len(groups) == 4 else (1, 0)
            metadata['parameters'] = [groups[i] for i in permutation]
            for name in metadata['attributes']:
                dataset[name] = np.transpose(dataset[name], permutation)
        actual, _ = rcwa.convert_native(self.datasets, self.model, self.settings)
        for name in ('r_E', 't_E', 'S11', 'S21'):
            np.testing.assert_allclose(actual[name], expected[name])

    def test_missing_native_dataset_or_attribute_is_refused(self):
        for name in ('grating_characterization', 'grating_orders', 'field_amplitudes', 'total_energy'):
            with self.subTest(dataset=name):
                altered = copy.deepcopy(self.datasets)
                del altered[name]
                with self.assertRaises((ValueError, KeyError)):
                    rcwa.convert_native(altered, self.model, self.settings)
        for dataset, attribute in (('grating_characterization', 'Rps'),
                                   ('field_amplitudes', 'ekz_bp'), ('grating_orders', 'kx')):
            with self.subTest(attribute=attribute):
                altered = copy.deepcopy(self.datasets)
                del altered[dataset][attribute]
                with self.assertRaises(ValueError):
                    rcwa.convert_native(altered, self.model, self.settings)

    def test_guessed_shapes_missing_metadata_and_wrong_coordinates_are_refused(self):
        for change in ('metadata', 'shape', 'wavelength', 'invariant_order', 'complex_kx', 'wrong_kx'):
            with self.subTest(change=change):
                altered = copy.deepcopy(self.datasets)
                gc = altered['grating_characterization']
                if change == 'metadata':
                    del gc['Lumerical_dataset']
                elif change == 'shape':
                    gc['Rss'] = gc['Rss'].squeeze()
                elif change == 'wavelength':
                    gc['lambda'][:] = 650e-9
                elif change == 'invariant_order':
                    gc['m'][:] = 1
                elif change == 'complex_kx':
                    altered['grating_orders']['kx'] = altered['grating_orders']['kx'] + 1e-3j
                else:
                    altered['grating_orders']['kx'] = altered['grating_orders']['kx'] + 1e-3
                with self.assertRaises(ValueError):
                    rcwa.convert_native(altered, self.model, self.settings)

    def test_cartesian_projection_grating_power_and_energy_are_independent_gates(self):
        for change in ('cartesian', 'grating', 'total_energy', 'closure'):
            with self.subTest(change=change):
                altered = copy.deepcopy(self.datasets)
                center = int(np.flatnonzero(self.orders == 0)[0])
                if change == 'cartesian':
                    altered['field_amplitudes']['ekx_bp'][0, 0, center, 0] += .01
                elif change == 'grating':
                    altered['grating_orders']['Rs_grating'][0, 0, center, 0] += .01
                elif change == 'total_energy':
                    altered['total_energy']['Rs'] += .01
                else:
                    # Rescale all amplitudes and matching powers together: the
                    # Cartesian and per-order power checks still agree, while
                    # independent unit-flux closure must reject this fixture.
                    for name in altered['grating_characterization']['Lumerical_dataset']['attributes']:
                        altered['grating_characterization'][name] *= .9
                    for name in altered['field_amplitudes']['Lumerical_dataset']['attributes']:
                        altered['field_amplitudes'][name] *= .9
                    for name in altered['grating_orders']['Lumerical_dataset']['attributes']:
                        if name not in ('kx', 'ky'):
                            altered['grating_orders'][name] *= .81
                    for name in altered['total_energy']['Lumerical_dataset']['attributes']:
                        altered['total_energy'][name] *= .81
                with self.assertRaises(ValueError):
                    rcwa.convert_native(altered, self.model, self.settings)


class TestRCWAClosedCalibrationGate(unittest.TestCase):
    def setUp(self):
        self.model = model_fixture()
        self.settings = settings_fixture()
        self.version = '8.34.4175'

    def test_absent_failed_or_merely_asserted_pass_cannot_open_gate(self):
        for certificate in (None, {}, {'status': 'FAIL'}, {'status': 'PASS'}):
            with self.subTest(certificate=certificate):
                with self.assertRaises(ValueError):
                    rcwa.require_phase_calibration(certificate, self.model, self.settings, self.version)

    def test_stale_scope_version_settings_and_missing_evidence_are_refused(self):
        certificate = {'status': 'PASS', 'backend_fingerprint': rcwa.backend_fingerprint(),
                       'scope': rcwa.calibration_scope(self.model), 'lumerical_version': self.version,
                       'levels': [self.settings], 'registration': 'half_cell', 'evidence': []}
        for change in ({'backend_fingerprint': 'old-backend'}, {'scope': {}},
                       {'lumerical_version': '2023 R2.1'}, {'levels': []},
                       {'registration': 'fitted_phase'}, {}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    rcwa.require_phase_calibration(dict(certificate, **change), self.model,
                                                   self.settings, self.version)

    def mocked_certificate(self):
        """Gate fixture only; no real acquisition or certificate is produced."""
        return {'schema_version': '2.0', 'status': 'PASS', 'backend_fingerprint': rcwa.backend_fingerprint(),
                'absolute_phase_status': 'VERIFIED_ABSOLUTE_X0',
                'scope': rcwa.calibration_scope(self.model), 'lumerical_version': self.version,
                'levels': [self.settings], 'registration': 'cell_origin_half_cell',
                'absolute_origin': {'status': 'PASS', 'registration': 'cell_origin_half_cell',
                                    'source_sha256': 'mocked-source',
                                    'evidence': [{'folder': f'memory_only_born_{i}', 'input_sha256': 'mocked-input',
                                                  'native_status_sha256': 'mocked-status'} for i in range(4)]},
                'checks': [{'name': 'mocked_math_check', 'status': 'PASS'}],
                'convergence_tolerances': {'complex_relative': .03},
                'evidence': [{'folder': f'memory_only_control_{i}', 'input_sha256': 'mocked-input',
                              'native_status_sha256': 'mocked-status'} for i in range(6)]}

    def test_gate_requires_recomputed_certificate_fields_not_just_checks(self):
        reference = self.mocked_certificate()
        mocked_acquisition = ({}, {'input_sha256': 'mocked-input'}, {})
        for change in ('scope_and_model', 'level_and_request', 'version_and_request',
                       'convergence_tolerance', 'extra_assertion', 'origin_source', 'origin_proof'):
            with self.subTest(change=change):
                certificate = copy.deepcopy(reference)
                model = copy.deepcopy(self.model)
                settings = dict(self.settings)
                version = self.version
                if change == 'scope_and_model':
                    model['wavelength_nm'] = 650
                    certificate['scope'] = rcwa.calibration_scope(model)
                elif change == 'level_and_request':
                    settings['mesh_cells_x'] = 1600
                    certificate['levels'] = [settings]
                elif change == 'version_and_request':
                    version = '8.34.9999'
                    certificate['lumerical_version'] = version
                elif change == 'convergence_tolerance':
                    certificate['convergence_tolerances']['complex_relative'] = .99
                elif change == 'origin_source':
                    certificate['absolute_origin']['source_sha256'] = 'edited-source'
                elif change == 'origin_proof':
                    certificate['absolute_origin']['tolerances'] = {'complex_relative': .99}
                else:
                    certificate['unsupported_claim'] = 'calibrated for an unrelated family'
                with patch.object(rcwa, 'sha256', return_value='mocked-status'), \
                        patch.object(rcwa, 'read_acquisition', return_value=mocked_acquisition), \
                        patch.object(rcwa, 'calibrate', return_value=reference) as recompute:
                    with self.assertRaisesRegex(ValueError, 'no longer reproduces'):
                        rcwa.require_phase_calibration(certificate, model, settings, version)
                    recompute.assert_called_once()

    def test_identical_recomputed_gate_fixture_does_not_export_results(self):
        certificate = self.mocked_certificate()
        # Isolate the certificate comparison only. Actual evidence existence,
        # checksums and conversion have independent refusal tests above.
        with patch.object(rcwa, 'sha256', return_value='mocked-status'), \
                patch.object(rcwa, 'read_acquisition', return_value=(
                    {}, {'input_sha256': 'mocked-input'}, {})), \
                patch.object(rcwa, 'calibrate', return_value=copy.deepcopy(certificate)) as recompute:
            rcwa.require_phase_calibration(certificate, self.model, self.settings, self.version)
            recompute.assert_called_once_with([item['folder'] for item in certificate['evidence']],
                                              'cell_origin_half_cell',
                                              [item['folder'] for item in certificate['absolute_origin']['evidence']])

    def test_old_relative_registration_cannot_claim_absolute_x0(self):
        for registration in ('native', 'half_cell'):
            certificate = self.mocked_certificate()
            certificate['registration'] = registration
            with self.subTest(registration=registration), self.assertRaisesRegex(ValueError, 'Absolute physical x=0'):
                rcwa.require_phase_calibration(certificate, self.model, self.settings, self.version)

    def test_missing_born_evidence_and_off_grid_target_are_refused(self):
        certificate = self.mocked_certificate()
        certificate['absolute_origin']['evidence'] = []
        with self.assertRaisesRegex(ValueError, 'four real'):
            rcwa.require_phase_calibration(certificate, self.model, self.settings, self.version)
        model = dict(self.model, ridge_shift_nm=1)
        with self.assertRaisesRegex(ValueError, 'off-grid'):
            rcwa.require_phase_calibration(self.mocked_certificate(), model, self.settings, self.version)

    def test_each_born_raw_hash_failure_refuses_before_certificate_recomputation(self):
        certificate = self.mocked_certificate()
        for damaged in range(4):
            wanted = certificate['absolute_origin']['evidence'][damaged]['folder']
            def read(folder):
                if str(folder) == wanted:
                    raise rcwa.RCWAError('Native artifact checksum mismatch: native_index.npz')
                return {}, {'input_sha256': 'mocked-input'}, {}
            with self.subTest(damaged=damaged), \
                    patch.object(rcwa, 'sha256', return_value='mocked-status'), \
                    patch.object(rcwa, 'read_acquisition', side_effect=read), \
                    patch.object(rcwa, 'calibrate') as recompute:
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    rcwa.require_phase_calibration(certificate, self.model, self.settings, self.version)
                recompute.assert_not_called()

    def test_missing_real_acquisitions_never_create_success_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            with self.assertRaises((ValueError, FileNotFoundError)):
                rcwa.export_calibrated(folder, {'status': 'PASS'})
            self.assertFalse(list(folder.rglob('result.npz')))
            self.assertFalse(list(folder.rglob('rcwa_coefficients.npz')))


class TestRCWAConvergenceGate(unittest.TestCase):
    def calibrate_math_controls(self, disagreement=None, origin_status='PASS', registration='cell_origin_half_cell'):
        """Exercise calibration orchestration with in-memory converted controls.

        Native conversion and evidence I/O are mocked at their boundaries;
        synthetic controls cannot become a saved PASS certificate or result.
        """
        base = model_fixture()
        r_orders, t_orders = np.arange(-24, 25), np.arange(-37, 38)
        r = np.zeros((len(r_orders), 2, 2), dtype=complex)
        for i, order in enumerate(r_orders):
            if abs(order) in (0, 1, 5):
                r[i] = np.eye(2) * (.05 + abs(order) * .001) * np.exp(.1j)
        q = np.sqrt(1 - (r_orders * 643 / 16000) ** 2)
        r_total = float(np.sum(q * np.abs(r[:, 0, 0]) ** 2))
        t = np.zeros((len(t_orders), 2, 2), dtype=complex)
        t[t_orders == 0] = np.eye(2) * np.sqrt((1 - r_total) / 1.5) * np.exp(.13j)
        bare_arrays = {'r_orders': r_orders, 't_orders': t_orders, 'r_E': r, 't_E': t,
                       'eta_r_s': q * np.abs(r[:, 0, 0]) ** 2,
                       'eta_r_p': q * np.abs(r[:, 1, 1]) ** 2}
        bare_diagnostic = {'total_power': {'Rs': r_total, 'Rp': r_total,
                                           'Ts': 1 - r_total, 'Tp': 1 - r_total}}
        planar_arrays = copy.deepcopy(bare_arrays)
        planar_arrays['r_E'][:] = 0
        planar_arrays['t_E'][:] = 0
        planar_arrays['r_E'][r_orders == 0] = np.eye(2) * (-.2 * np.exp(4j * np.pi * 1800 / 643))
        planar_arrays['t_E'][t_orders == 0] = np.eye(2) * (.8 * np.exp(2j * np.pi * 3375 / 643))
        for pol in 'sp':
            planar_arrays['eta_r_' + pol][:] = 0
            planar_arrays['eta_r_' + pol][r_orders == 0] = .04
        planar_diagnostic = {'total_power': {'Rs': .04, 'Rp': .04, 'Ts': .96, 'Tp': .96}}
        records, converted, folders = {}, {}, []
        for name, mesh, truncation in (('coarse', 800, 80), ('refined', 1600, 160)):
            settings = dict(settings_fixture(), name=name, mesh_cells_x=mesh,
                            max_number_k_vectors=truncation)
            for control in ('planar', 'bare_zero', 'bare_moved'):
                model = copy.deepcopy(base)
                model['geometry_kind'] = 'planar_interface' if control == 'planar' else 'ah53'
                if control == 'bare_moved':
                    model['ridge_shift_nm'] = 100
                arrays = copy.deepcopy(planar_arrays if control == 'planar' else bare_arrays)
                diagnostic = copy.deepcopy(planar_diagnostic if control == 'planar' else bare_diagnostic)
                if control == 'bare_moved':
                    for side in 'rt':
                        factor = np.exp(-2j * np.pi * arrays[side + '_orders'] * 100 / 16000)
                        arrays[side + '_E'] *= factor[:, None, None]
                if name == 'refined' and control != 'planar':
                    if disagreement == 'complex':
                        # A common phase offset preserves same-mesh mirror,
                        # translation, magnitudes and total powers.
                        arrays['r_E'] *= np.exp(.05j)
                    elif disagreement == 'power':
                        # Isolate the comparison of already checked per-run
                        # totals from the independent native conversion gate.
                        for pol in 'sp':
                            diagnostic['total_power']['R' + pol] += .02
                            diagnostic['total_power']['T' + pol] -= .02
                key = f'memory_only_{name}_{control}'
                folders.append(key)
                frozen = {'case': {'model': model, 'digest': control}, 'settings': settings}
                state = {'input_sha256': rcwa.canonical_hash(frozen), 'lumerical_version': '8.34.4175',
                         'backend_fingerprint': rcwa.backend_fingerprint(), 'file_sha256': {'memory_only': 'not-real-data'}}
                records[key] = (frozen, state, {'mock_control_key': key})
                converted[key] = (arrays, diagnostic)
        with patch.object(rcwa, 'read_acquisition', side_effect=lambda folder: records[str(folder)]), \
                patch.object(rcwa, 'sha256', return_value='mocked-status'), \
                patch.object(rcwa, 'convert_native', side_effect=lambda datasets, *args: converted[datasets['mock_control_key']]), \
                patch.object(rcwa, 'verify_absolute_origin', return_value={
                    'status': origin_status, 'registration': 'cell_origin_half_cell', 'evidence': []}):
            return rcwa.calibrate(folders, registration, ['memory_only_born']*4)

    def test_same_mesh_controls_and_matching_two_levels_pass_in_memory(self):
        report = self.calibrate_math_controls()
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(len(report['evidence']), 6)
        self.assertEqual(len(report['checks']), 43)
        self.assertTrue(all(item['status'] == 'PASS' for item in report['checks']))

    def test_born_failure_or_old_relative_registration_cannot_be_hidden_by_43_passes(self):
        for status, registration in (('FAIL', 'cell_origin_half_cell'), ('PASS', 'native'), ('PASS', 'half_cell')):
            with self.subTest(status=status, registration=registration):
                report = self.calibrate_math_controls(origin_status=status, registration=registration)
                self.assertTrue(all(item['status'] == 'PASS' for item in report['checks']))
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual(report['absolute_phase_status'], 'UNVERIFIED')

    def test_complex_mesh_disagreement_fails_within_level_controls_still_passing(self):
        report = self.calibrate_math_controls('complex')
        self.assertEqual(report['status'], 'FAIL')
        failed = [item for item in report['checks'] if item['status'] == 'FAIL']
        self.assertTrue(failed)
        self.assertTrue(all('complex_relative_convergence' in item['name'] for item in failed))
        self.assertTrue(all(item['error'] > .03 for item in failed))

    def test_total_power_disagreement_fails_independently_of_complex_comparison(self):
        report = self.calibrate_math_controls('power')
        self.assertEqual(report['status'], 'FAIL')
        failed = [item for item in report['checks'] if item['status'] == 'FAIL']
        self.assertTrue(failed)
        self.assertTrue(all('total_power_absolute_convergence' in item['name'] for item in failed))
        self.assertTrue(all(item['error'] > .01 for item in failed))


if __name__ == '__main__':
    unittest.main()
