"""Absolute transverse phase tests using Born arrays only in memory.

Evidence I/O and native conversion are mocked at their boundaries. No CAD,
solver result, PASS certificate, archive, or diagnostic report is created.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from ah53runner import rcwa
from tools import validate_rcwa_origin as origin


ROOT = Path(__file__).resolve().parents[1]
VERSION = '8.34.4175'


def origin_controls(registration='cell_origin_half_cell', nonlinear_strong=False):
    config = json.loads((ROOT / 'configs' / 'rcwa_origin_validation.json').read_text(encoding='utf8'))
    levels = copy.deepcopy(config['rcwa_levels'])
    base = copy.deepcopy(config['base_model'])
    target = copy.deepcopy(base)
    target.update(n_substrate=1.5, n_ridge=2., ridge_height_nm=300)
    scope = rcwa.calibration_scope(target)
    folders, acquisitions, converted = [], {}, {}
    orders = np.arange(-24, 25)
    for level in levels:
        for contrast, index in (('strong', 1.0001), ('weak', 1.00005)):
            model = copy.deepcopy(base)
            model['n_ridge'] = index
            case = {'id': f'memory_born_{contrast}', 'model': model,
                    'digest': rcwa.canonical_hash(model)}
            frozen = {'case': case, 'settings': copy.deepcopy(level),
                      'geometry': rcwa.geometry_spec(model)}
            key = str((ROOT / 'memory_only_origin_tests' / case['id'] / level['name']).resolve())
            folders.append(key)
            state = {'input_sha256': rcwa.canonical_hash(frozen),
                     'backend_fingerprint': rcwa.backend_fingerprint(),
                     'lumerical_version': VERSION,
                     'file_sha256': {'not_a_real_acquisition': 'mocked-evidence-only'}}
            # Divide by the declared coordinate conversion to construct raw
            # native amplitudes; evaluate_origin must recover absolute Born
            # phases using the independently implemented candidate formula.
            factor = origin.hypothesis_factor(model, level, orders, registration)
            native = origin.born_reflection_s(model, orders) / factor
            if nonlinear_strong and contrast == 'strong':
                native *= 1.007
            electric = np.zeros((len(orders), 2, 2), dtype=complex)
            electric[:, 0, 0] = native
            ct = np.sqrt(1 - (orders * model['wavelength_nm'] / model['period_nm']) ** 2)
            electric[:, 1, 1] = ct * native  # p is a diagnostic in this proof.
            arrays = {'r_orders': orders.copy(), 'r_E': electric}
            acquisitions[key] = (frozen, state, {'mock_control_key': key})
            converted[key] = (arrays, {'phase_status': 'UNCALIBRATED_DIAGNOSTIC_ONLY'})
    return folders, levels, scope, acquisitions, converted


def run_origin_fixture(folders, levels, scope, acquisitions, converted, version=VERSION):
    with patch.object(rcwa, 'read_acquisition', side_effect=lambda folder: acquisitions[str(folder)]), \
            patch.object(rcwa, 'convert_native', side_effect=lambda data, *args: converted[data['mock_control_key']]), \
            patch.object(rcwa, 'sha256', return_value='mocked-source-and-evidence-hash'), \
            patch.object(origin, 'validate', side_effect=AssertionError('Saved origin verdict must not be read')) as report_cli:
        proof = rcwa.verify_absolute_origin(folders, levels, scope, version)
        report_cli.assert_not_called()
        return proof


class TestRCWAAbsoluteOrigin(unittest.TestCase):
    def test_registration_uses_full_cell_origin_and_actual_half_cell(self):
        orders = np.array([-5, -2, -1, 0, 1, 2, 5])
        for cells in (800, 1600):
            dx = 16000 / cells
            expected = np.exp(-2j * np.pi * orders * (-8000 + dx / 2) / 16000)
            actual = rcwa.registration_factor(orders, 16000, cells, 'cell_origin_half_cell')
            np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-14)
            np.testing.assert_allclose(np.abs(actual), 1., rtol=0, atol=2e-15)
            old_half = rcwa.registration_factor(orders, 16000, cells, 'half_cell')
            np.testing.assert_allclose(actual / old_half, (-1.) ** orders, atol=1e-14)

    def test_absolute_born_controls_have_one_cell_origin_winner(self):
        report = run_origin_fixture(*origin_controls())
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['registration'], 'cell_origin_half_cell')
        self.assertEqual(report['passing_hypotheses'], ['cell_origin_half_cell'])
        self.assertEqual(report['source_sha256'], 'mocked-source-and-evidence-hash')
        self.assertIn('-period/2+dx/2', report['formula'])
        self.assertEqual(report['orders'], list(origin.ORDERS))
        self.assertEqual(report['tolerances'], origin.TOLERANCES)
        self.assertEqual(len(report['evidence']), 4)
        self.assertEqual({item['settings']['mesh_cells_x'] for item in report['evidence']}, {800, 1600})
        self.assertEqual(report['candidates']['native']['status'], 'FAIL')
        self.assertEqual(report['candidates']['half_cell']['status'], 'FAIL')
        self.assertTrue(all(item['status'] == 'PASS' for item in
                            report['candidates']['cell_origin_half_cell']['checks']))

    def test_native_or_old_half_cell_winner_cannot_certify_absolute_phase(self):
        for old_registration in ('native', 'half_cell'):
            with self.subTest(registration=old_registration):
                report = run_origin_fixture(*origin_controls(old_registration))
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual(report['registration'], old_registration)
                self.assertEqual(report['passing_hypotheses'], [old_registration])
                self.assertEqual(report['candidates']['cell_origin_half_cell']['status'], 'FAIL')

    def test_chi_linearity_breaks_despite_each_absolute_born_error_below_one_percent(self):
        report = run_origin_fixture(*origin_controls(nonlinear_strong=True))
        self.assertEqual(report['status'], 'FAIL')
        checks = report['candidates']['cell_origin_half_cell']['checks']
        failures = [item for item in checks if item['status'] == 'FAIL']
        self.assertEqual(len(failures), 2)
        self.assertTrue(all('chi_halving_scaled_complex_relative' in item['name'] for item in failures))
        self.assertTrue(all(item['value'] > .005 for item in failures))
        self.assertTrue(all(item['status'] == 'PASS' for item in checks
                            if 'chi_halving_scaled_complex_relative' not in item['name']))

    def test_four_distinct_contrasts_and_two_main_mesh_levels_are_required(self):
        for change in ('missing_folder', 'duplicate_folder', 'duplicate_contrast', 'missing_level'):
            with self.subTest(change=change):
                folders, levels, scope, acquisitions, converted = origin_controls()
                if change == 'missing_folder':
                    folders.pop()
                elif change == 'duplicate_folder':
                    folders[-1] = folders[0]
                elif change == 'duplicate_contrast':
                    model = acquisitions[folders[1]][0]['case']['model']
                    model['n_ridge'] = 1.0001
                    acquisitions[folders[1]][0]['geometry'] = rcwa.geometry_spec(model)
                else:
                    levels.pop()
                with self.assertRaises(ValueError):
                    run_origin_fixture(folders, levels, scope, acquisitions, converted)

    def test_scope_version_settings_and_geometry_mismatch_are_refused(self):
        for change in ('scope', 'version', 'settings', 'geometry', 'contrast',
                       'height', 'substrate', 'normalized_model'):
            with self.subTest(change=change):
                folders, levels, scope, acquisitions, converted = origin_controls()
                frozen, state, _ = acquisitions[folders[-1]]
                model = frozen['case']['model']
                if change == 'scope':
                    scope['wavelength_nm'] = 650
                elif change == 'version':
                    state['lumerical_version'] = '8.34.9999'
                elif change == 'settings':
                    frozen['settings']['max_number_k_vectors'] += 1
                elif change == 'geometry':
                    frozen['geometry']['reflection_reference_z_nm'] = -1790
                elif change == 'contrast':
                    model['n_ridge'] = 1.0002
                elif change == 'height':
                    model['ridge_height_nm'] = 101
                elif change == 'substrate':
                    model['n_substrate'] = 1.5
                else:
                    model['mesh']['dx_nm'] = 45
                with self.assertRaises(ValueError):
                    run_origin_fixture(folders, levels, scope, acquisitions, converted)


class TestRCWANativeIndexOrigin(unittest.TestCase):
    def setUp(self):
        config = json.loads((ROOT / 'configs' / 'rcwa_origin_validation.json').read_text(encoding='utf8'))
        self.model = config['base_model']
        self.settings = config['rcwa_levels'][0]

    def index_fixture(self):
        count = self.settings['mesh_cells_x']
        period = self.model['period_nm'] * 1e-9
        return {'Lumerical_dataset': {'geometry': 'rectilinear'},
                'x': (-period / 2 + (np.arange(count) + .5) * period / count)[:, None]}

    def test_declared_cell_center_sample_coordinates_are_verified(self):
        diagnostic = rcwa.validate_native_transverse_origin(self.index_fixture(), self.model, self.settings)
        self.assertEqual(diagnostic['mesh_cells_x'], 800)
        self.assertAlmostEqual(diagnostic['first_x_nm'], -7990)
        self.assertAlmostEqual(diagnostic['last_x_nm'], 7990)
        self.assertAlmostEqual(diagnostic['cell_width_nm'], 20)

    def test_missing_or_shifted_native_index_registration_is_refused(self):
        for change in ('missing_metadata', 'missing_x', 'sample_edges', 'missing_sample',
                       'nonuniform', 'nonfinite', 'complex'):
            with self.subTest(change=change):
                dataset = self.index_fixture()
                if change == 'missing_metadata':
                    dataset.pop('Lumerical_dataset')
                elif change == 'missing_x':
                    dataset.pop('x')
                elif change == 'sample_edges':
                    dataset['x'] -= 10e-9
                elif change == 'missing_sample':
                    dataset['x'] = dataset['x'][:-1]
                elif change == 'nonuniform':
                    dataset['x'][400] += 1e-9
                elif change == 'nonfinite':
                    dataset['x'][400] = np.nan
                else:
                    dataset['x'] = dataset['x'] + 1e-9j
                with self.assertRaises(ValueError):
                    rcwa.validate_native_transverse_origin(dataset, self.model, self.settings)


if __name__ == '__main__':
    unittest.main()
