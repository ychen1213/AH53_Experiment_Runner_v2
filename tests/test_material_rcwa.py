"""Offline material/RCWA boundaries. All numeric datasets here are synthetic."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from ah53runner import rcwa
from tests.test_rcwa import model_fixture, settings_fixture, synthetic_datasets
from tools import compare_backends


def named_model(planar=False):
    model = model_fixture()
    model.pop('n_substrate')
    model['substrate_material'] = 'In-memory test material'
    model['geometry_kind'] = 'planar_interface' if planar else 'ah53'
    return model


def resolution(model, n=1.5, k=0.):
    roles = {'substrate': {'kind': 'database', 'name': model['substrate_material'],
                          'n': n, 'k': k, 'raw_n': n, 'raw_k': k,
                          'validation': {'status': 'PASS', 'cartesian_components_checked': [1, 2, 3]}}}
    if model['geometry_kind'] != 'planar_interface':
        roles['ridge'] = {'kind': 'constant', 'n': model['n_ridge'], 'k': 0.}
    return {'roles': roles, 'material_databases': [], 'fit_wavelength_range_nm': [543., 743.],
            'target_wavelength_nm': 643.}


def index_fixture(spec):
    values = [1+0j] + [complex(box['n'], box.get('k', 0)) for box in spec['boxes']]
    return {'Lumerical_dataset': {'attributes': ['index_x', 'index_y', 'index_z']},
            **{key: np.asarray(values, complex)[:, None] for key in ('index_x', 'index_y', 'index_z')}}


class MaterialGeometryTests(unittest.TestCase):
    def test_named_index_is_unknown_until_real_resolution(self):
        model = named_model()
        planned = rcwa.geometry_spec(model)
        self.assertIsNone(planned['n_substrate'])
        self.assertIsNone(planned['boxes'][0]['n'])
        self.assertEqual(planned['boxes'][0]['material_name'], model['substrate_material'])
        actual = rcwa.resolved_geometry(planned, resolution(model), settings_fixture())
        self.assertEqual(actual['boxes'][0]['n'], 1.5)
        self.assertFalse(actual['absorbing_substrate'])
        self.assertEqual(planned['interfaces_nm'], actual['interfaces_nm'])
        self.assertIsNone(planned['boxes'][0]['n'])  # resolution cannot mutate frozen input

    def test_lossy_final_halfspace_is_replaced_by_explicit_layer_and_real_terminal(self):
        model = named_model()
        planned = rcwa.geometry_spec(model)
        settings = dict(settings_fixture(), substrate_depth_nm=3000., termination_index=1.2)
        actual = rcwa.resolved_geometry(planned, resolution(model, n=3., k=.2), settings)
        double = rcwa.resolved_geometry(planned, resolution(model, n=3., k=.2), settings, 2.)
        self.assertTrue(actual['absorbing_substrate'])
        self.assertEqual(actual['boxes'][0]['z_max_nm'], 3000.)
        self.assertEqual(actual['boxes'][0]['k'], .2)
        self.assertEqual(actual['boxes'][-1]['z_min_nm'], 3000.)
        self.assertEqual(actual['boxes'][-1]['k'], 0.)
        self.assertNotIn('material_name', actual['boxes'][-1])
        self.assertEqual(actual['terminal_index'], 1.2)
        self.assertEqual(double['substrate_depth_nm'], 6000.)
        self.assertEqual(actual['internal_fdtd_transmission_z_nm'], 1050.)
        self.assertEqual(actual['transmission_reference_z_nm'], 3000.)
        self.assertIn('artificial_lossless_terminal', actual['transmission_scope'])

    def test_depth_and_resolved_terminal_sampling_are_checked(self):
        model = named_model()
        for changes in ({'substrate_depth_nm': 1000.}, {'termination_index': 5.}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                rcwa.resolved_geometry(rcwa.geometry_spec(model), resolution(model, k=.1),
                                       dict(settings_fixture(), **changes))

    def test_only_lossless_raw_data_can_use_declared_tiny_fit_loss_terminal(self):
        model = named_model()
        material = resolution(model, k=1.53e-10)
        material['roles']['substrate']['raw_k'] = 0.
        actual = rcwa.resolved_geometry(rcwa.geometry_spec(model), material, settings_fixture())
        self.assertFalse(actual['absorbing_substrate'])
        self.assertEqual(actual['boxes'][0]['k'], 1.53e-10)
        self.assertTrue(actual['terminal_negligible_fit_loss_approximation']['applied'])
        self.assertLess(actual['terminal_negligible_fit_loss_approximation']['air_interface_complex_Fresnel_difference'], 1e-9)
        self.assertLess(actual['terminal_negligible_fit_loss_approximation']['single_pass_intensity_loss_over_internal_depth'], 1e-8)
        material['roles']['substrate']['raw_k'] = 1e-12
        absorbing = rcwa.resolved_geometry(rcwa.geometry_spec(model), material, settings_fixture())
        self.assertTrue(absorbing['absorbing_substrate'])
        self.assertFalse(absorbing['terminal_negligible_fit_loss_approximation']['applied'])
        material['roles']['substrate']['k'] = 0.
        with self.assertRaisesRegex(ValueError, 'fitted target k is not positive'):
            rcwa.resolved_geometry(rcwa.geometry_spec(model), material, settings_fixture())

    def test_build_preserves_imported_material_and_matches_fitting_band(self):
        model = named_model()
        spec = rcwa.geometry_spec(model)
        spec['material_databases'] = [{'path': 'C:/材料/test only/material library.mdf', 'sha256': 'fixture'}]
        standalone = rcwa.build_lsf(spec, settings_fixture())
        self.assertLess(standalone.index('newproject;'), standalone.index('importmaterialdb'))
        self.assertIn('C:/材料/test only/material library.mdf', standalone)
        self.assertNotIn('\\u6750', standalone)
        self.assertIn('set("material","In-memory test material")', standalone)
        actual = rcwa.resolved_geometry(spec, resolution(model, k=.1), settings_fixture())
        script = rcwa.build_lsf(actual, settings_fixture(), new_project=False, run=False)
        self.assertNotIn('newproject', script)
        self.assertNotIn('run("RCWA")', script)
        for prop in ('fit materials with multi-coefficient model', 'fit sampled materials',
                     'fit analytic materials', 'override materials frequency range'):
            self.assertIn('set("'+prop+'",1);', script)
        self.assertIn('materials minimum wavelength', script)
        self.assertIn('materials maximum wavelength', script)


class MaterialPhysicsTests(unittest.TestCase):
    def test_actual_solver_string_mesh_readback_is_numeric_but_still_exact(self):
        model = named_model(planar=True)
        settings = dict(settings_fixture(), mesh_cells_x=256)
        spec = rcwa.resolved_geometry(rcwa.geometry_spec(model), resolution(model), settings)
        state = {'actual_material_assignments': {
            box['name']: box.get('material_name', '<Object defined dielectric>') for box in spec['boxes']},
            'actual_material_fit_settings': {
                'fit materials with multi-coefficient model': 1., 'fit sampled materials': 1.,
                'fit analytic materials': 1., 'override materials frequency range': 1.,
                'materials minimum wavelength': 543e-9, 'materials maximum wavelength': 743e-9},
            'actual_solver': {'actual mesh cells x': '256',
                             'interface actual positions': np.asarray(spec['interfaces_nm']).reshape(-1, 1)*1e-9}}
        rcwa.verify_actual_material_settings(state, spec, settings)
        with self.assertRaisesRegex(ValueError, 'mesh differs'):
            rcwa.verify_actual_material_settings(state, spec, dict(settings, mesh_cells_x=160))

    def test_observed_v252_scalar_packed_layout_requires_separate_isotropy_evidence(self):
        model = named_model(planar=True)
        spec = rcwa.resolved_geometry(rcwa.geometry_spec(model), resolution(model), settings_fixture())
        # Layout observed in archived v252 planar acquisitions: index_z only,
        # 800 x samples * four layer intervals, retained as (3200,1,1).
        data = {'Lumerical_dataset': {'parameters': [['lambda', 'f']],
                    'attributes': ['index_z'], 'geometry': 'rectilinear'},
                'index_z': np.r_[np.ones(1600), np.full(1600, 1.5)].reshape(3200, 1, 1).astype(complex)}
        proof = rcwa.validate_material_index(data, spec)
        self.assertEqual(proof['native_index_representation'], 'scalar_isotropic_index_z')
        self.assertNotIn('index_x', data)
        self.assertNotIn('index_y', data)
        altered = copy.deepcopy(spec)
        del altered['material_resolution']['roles']['substrate']['validation']
        with self.assertRaisesRegex(ValueError, 'independent three-axis'):
            rcwa.validate_material_index(data, altered)
        data['Lumerical_dataset']['attributes'] = ['index_x']
        with self.assertRaisesRegex(ValueError, 'scalar isotropic index_z'):
            rcwa.validate_material_index(data, spec)

    def test_native_index_detects_wrong_fit_missing_material_and_anisotropy(self):
        model = named_model()
        spec = rcwa.resolved_geometry(rcwa.geometry_spec(model), resolution(model, 3., .2), settings_fixture())
        data = index_fixture(spec)
        self.assertEqual(rcwa.validate_material_index(data, spec)['status'], 'PASS')
        for mode in ('wrong_fit', 'missing', 'anisotropic'):
            altered = copy.deepcopy(data)
            if mode == 'anisotropic':
                altered['index_y'][1] += .1
            else:
                for key in ('index_x', 'index_y', 'index_z'):
                    altered[key][1] = 1. if mode == 'missing' else 3.+.3j
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                rcwa.validate_material_index(altered, spec)

    def test_absorption_allows_residual_without_relaxing_lossless_energy_or_passivity(self):
        numeric = model_fixture()
        numeric['n_substrate'] = 1.
        data, _, _ = synthetic_datasets(numeric)
        for key in ('grating_characterization', 'field_amplitudes'):
            for name in data[key]['Lumerical_dataset']['attributes']:
                data[key][name] *= .8
        for key in ('grating_orders', 'total_energy'):
            for name in data[key]['Lumerical_dataset']['attributes']:
                if name not in ('kx', 'ky'):
                    data[key][name] *= .64
        model = named_model()
        spec = rcwa.resolved_geometry(rcwa.geometry_spec(model), resolution(model, k=.2), settings_fixture())
        arrays, diagnostic = rcwa.convert_native(data, model, settings_fixture(), resolved_spec=spec)
        self.assertAlmostEqual(diagnostic['unreturned_power']['s'], .36)
        self.assertEqual(diagnostic['transmission_scope'], spec['transmission_scope'])
        with self.assertRaisesRegex(ValueError, 'Lossless RCWA'):
            rcwa.convert_native(data, numeric, settings_fixture())
        with self.assertRaisesRegex(ValueError, 'actual resolved'):
            rcwa.convert_native(data, model, settings_fixture())
        for key in ('grating_characterization', 'field_amplitudes'):
            for name in data[key]['Lumerical_dataset']['attributes']:
                data[key][name] *= 2.
        for key in ('grating_orders', 'total_energy'):
            for name in data[key]['Lumerical_dataset']['attributes']:
                if name not in ('kx', 'ky'):
                    data[key][name] *= 4.
        with self.assertRaisesRegex(ValueError, 'Passive RCWA'):
            rcwa.convert_native(data, model, settings_fixture(), resolved_spec=spec)

    def test_depth_gate_checks_complex_reflection_even_when_power_matches(self):
        array = {'r_orders': np.array([-1, 0, 1]), 'r_E': np.ones((3, 2, 2), complex)*.1}
        diag = {'total_power': {'Rs': .04, 'Rp': .04}}
        self.assertEqual(rcwa.substrate_depth_convergence((array, diag), (array, diag))['status'], 'PASS')
        moved = dict(array, r_E=array['r_E'] * np.exp(.001j))
        self.assertEqual(rcwa.substrate_depth_convergence((array, diag), (moved, diag))['status'], 'FAIL')

    def test_comparison_refuses_different_library_or_fitting_band(self):
        left = named_model()
        right = copy.deepcopy(left)
        right['material_databases'] = [{'path': 'test.mdf', 'sha256': 'different'}]
        with self.assertRaisesRegex(ValueError, 'MDF content'):
            compare_backends.matching_geometry(left, right)
        right = copy.deepcopy(left)
        right['sim']['source_bandwidth_nm'] = 100
        with self.assertRaisesRegex(ValueError, 'fitting wavelength'):
            compare_backends.matching_geometry(left, right)


class SeparateMaterialGateTests(unittest.TestCase):
    def test_legacy_or_asserted_pass_cannot_export_named_materials(self):
        for certificate in ({'schema_version': '2.0', 'status': 'PASS'},
                            {'schema_version': 'materials-1.0', 'status': 'PASS'}):
            with self.subTest(certificate=certificate), self.assertRaises(ValueError):
                rcwa.require_material_calibration(certificate, 'missing')

    def test_certificate_cannot_authorize_another_target_or_edit_a_limit(self):
        folder = str(Path('memory_only_control').resolve())
        reference = {'schema_version': 'materials-1.0', 'status': 'PASS',
            'backend_fingerprint': rcwa.backend_fingerprint(),
            'evidence': [{'folder': folder, 'native_status_sha256': 'mock'}],
            'absolute_origin': None, 'tolerances': {'planar_complex_absolute': 1e-6}}
        with self.assertRaisesRegex(ValueError, 'exact real acquisitions'):
            rcwa.require_material_calibration(reference, 'another_target')
        modified = copy.deepcopy(reference)
        modified['tolerances']['planar_complex_absolute'] = 1.
        with patch.object(rcwa, 'sha256', return_value='mock'), \
                patch.object(rcwa, 'calibrate_materials', return_value=reference):
            with self.assertRaisesRegex(ValueError, 'does not reproduce'):
                rcwa.require_material_calibration(modified, folder)

    def test_planar_fresnel_gate_recomputes_complex_not_only_power(self):
        model = named_model(planar=True)
        settings = settings_fixture()
        resolved = resolution(model)
        actual = rcwa.resolved_geometry(rcwa.geometry_spec(model), resolved, settings)
        numeric = model_fixture()
        numeric['geometry_kind'] = 'planar_interface'
        datasets, _, _ = synthetic_datasets(numeric, planar_fresnel=True)
        frozen = {'case': {'model': model, 'id': 'mock_planar', 'digest': 'mock'},
                  'settings': settings, 'geometry': rcwa.geometry_spec(model)}
        state = {'resolved_geometry': actual, 'material_resolution': resolved,
                 'lumerical_version': '8.34.fixture', 'input_sha256': 'mock',
                 'backend_fingerprint': rcwa.backend_fingerprint(), 'file_sha256': {}}
        with patch.object(rcwa, 'read_acquisition', return_value=(frozen, state, datasets)), \
                patch.object(rcwa, 'sha256', return_value='mock'):
            self.assertEqual(rcwa.calibrate_materials(['memory_only'])['status'], 'PASS')
            for key in ('grating_characterization', 'field_amplitudes'):
                for name in datasets[key]['Lumerical_dataset']['attributes']:
                    datasets[key][name] *= np.exp(.01j)
            failed = rcwa.calibrate_materials(['memory_only'])
            self.assertEqual(failed['status'], 'FAIL')
            self.assertTrue(any(c['status'] == 'FAIL' and 'complex_Fresnel' in c['name'] for c in failed['checks']))


if __name__ == '__main__':
    unittest.main()
