"""Analytic, in-memory unit tests. No fabricated FDTD result files are written."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ah53runner.geometry import build_geometry
from ah53runner.planning import check_model, load_config, plan
from ah53runner.validation import (
    DEFAULT_TOLERANCES, check_common, check_planar, check_symmetry, check_translation,
    fresnel_at_reference, translation_factor, validate_results,
)

ROOT = Path(__file__).resolve().parents[1]


def analytic_coefficients(shift_nm=0, opposite_shift=False):
    """Pure math fixture for checking phase laws; never a solver result."""
    orders = np.arange(-5, 6)
    r = np.zeros((len(orders), 2, 2), dtype=complex)
    for i, m in enumerate(orders):
        factor = translation_factor(m, -shift_nm if opposite_shift else shift_nm, 16000)
        r[i, 0, 0] = (0.03 + abs(m) * 0.001) * factor
        r[i, 1, 1] = (0.04 + abs(m) * 0.001) * factor
    ct = np.sqrt(1 - (orders * 643 / 16000)**2)
    eta_s = ct * np.abs(r[:, 0, 0])**2
    eta_p = ct * np.abs(r[:, 1, 1])**2
    quality = {'incident_spatial_phase_sign': -1}
    for pol, eta in (('s', eta_s), ('p', eta_p)):
        quality.update({f'power_closure_{pol}': 1.,
                        f'total_reflected_{pol}': float(eta.sum()),
                        f'flux_decomposition_error_{pol}': 0.,
                        f'air_reference_reflected_{pol}': 0.})
    return {'orders': orders, 'r_E': r, 'lambda_nm': np.array([643.]),
            'theta_deg': np.rad2deg(np.arccos(ct)) * np.sign(orders),
            'r_power': r * np.sqrt(ct)[:, None, None],
            'eta_s': eta_s, 'eta_p': eta_p, 'quality': quality}


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.config, self.path = load_config(ROOT / 'configs' / 'physics_validation.json')
        self.tol = dict(DEFAULT_TOLERANCES)

    def test_four_case_plan_and_flat_interface_geometry(self):
        cases = plan(self.config)
        self.assertEqual(len(cases), 4)
        self.assertEqual([c['value'] for c in cases], [1.0, 1.5, 0, 100])
        for case in cases[:2]:
            shapes, top, meta = build_geometry(case['model'])
            self.assertEqual(top, 0)
            self.assertEqual(len(shapes), 1)
            self.assertEqual(meta['interface_y_nm'], 0)
            check_model(case['model'])

    def test_flat_interface_rejects_films_and_unknown_geometry(self):
        model = copy.deepcopy(self.config['base_model'])
        model['geometry_kind'] = 'planar_interface'
        model['films'] = [{'mode': 'planarized', 'thickness_nm': 10, 'n': 1.4}]
        with self.assertRaises(ValueError):
            build_geometry(model)
        model['geometry_kind'] = 'unknown'
        with self.assertRaises(ValueError):
            build_geometry(model)

    def test_fresnel_signs_and_reference_plane(self):
        self.assertAlmostEqual(fresnel_at_reference(1.5, 643, 0, -1), -0.2)
        self.assertAlmostEqual(fresnel_at_reference(1.5, 643, 0, 1), -0.2)
        self.assertAlmostEqual(fresnel_at_reference(1.5, 643, 0.643 / 8, -1), -0.2j)
        self.assertAlmostEqual(fresnel_at_reference(1.5, 643, 0.643 / 8, 1), 0.2j)
        self.assertEqual(fresnel_at_reference(1, 643, 1.8, -1), 0)
        with self.assertRaises(ValueError):
            fresnel_at_reference(1.5, 643, 1.8, 0)

    def test_planar_reference_rejects_wrong_p_sign(self):
        case = plan(self.config)[1]
        expected = fresnel_at_reference(1.5, 643, 1.8, -1)
        fixture = analytic_coefficients()
        fixture['r_E'][:] = 0
        i0 = int(np.flatnonzero(fixture['orders'] == 0)[0])
        fixture['r_E'][i0, 0, 0] = expected
        fixture['r_E'][i0, 1, 1] = -expected
        fixture['eta_s'][:] = 0
        fixture['eta_p'][:] = 0
        fixture['quality'].update({'total_reflected_s': 0.04, 'total_reflected_p': 0.04})
        checks = check_planar(case, fixture, self.tol)
        statuses = {c['name']: c['status'] for c in checks}
        self.assertEqual(statuses['fresnel_complex_s'], 'PASS')
        self.assertEqual(statuses['fresnel_complex_p'], 'FAIL')
        self.assertEqual(statuses['fresnel_phase_p'], 'FAIL')

    def test_signed_translation_law_both_polarizations(self):
        reference_case, moved_case = plan(self.config)[2:]
        reference = analytic_coefficients()
        moved = analytic_coefficients(100)
        checks = check_translation(reference_case, reference, moved_case, moved, self.tol)
        self.assertTrue(all(c['status'] == 'PASS' for c in checks))
        pair_deg = np.rad2deg(np.angle(translation_factor(2, 100, 16000)))
        self.assertAlmostEqual(pair_deg, -4.5)
        pair_deg5 = np.rad2deg(np.angle(translation_factor(10, 100, 16000)))
        self.assertAlmostEqual(pair_deg5, -22.5)
        opposite = analytic_coefficients(100, opposite_shift=True)
        checks = check_translation(reference_case, reference, moved_case, opposite, self.tol)
        self.assertTrue(any(c['status'] == 'FAIL' and 'phase' in c['name'] for c in checks))

    def test_zero_amplitude_phase_is_suppressed_and_cannot_pass_translation(self):
        reference_case, moved_case = plan(self.config)[2:]
        fixture = analytic_coefficients()
        fixture['r_E'][:] = 0
        checks = check_translation(reference_case, fixture, moved_case, fixture, self.tol)
        self.assertTrue(any(c['status'] == 'SUPPRESSED' for c in checks))
        self.assertTrue(any(c['status'] == 'FAIL' and 'observable' in c['name'] for c in checks))

    def test_symmetry_checks_polarizations_and_detects_asymmetry(self):
        fixture = analytic_coefficients()
        self.assertTrue(all(c['status'] == 'PASS' for c in check_symmetry(fixture, self.tol)))
        fixture['eta_p'][0] *= 2
        checks = check_symmetry(fixture, self.tol)
        self.assertEqual(next(c['status'] for c in checks if c['name'] == 'symmetry_power_p_m5'), 'FAIL')

    def test_reference_leakage_required_and_threshold_enforced(self):
        fixture = analytic_coefficients()
        self.assertTrue(all(c['status'] == 'PASS' for c in check_common(fixture, self.tol)))
        fixture['quality']['air_reference_reflected_p'] = 2e-6
        checks = check_common(fixture, self.tol)
        self.assertEqual(next(c['status'] for c in checks if c['name'] == 'air_reference_leakage_p'), 'FAIL')
        del fixture['quality']['air_reference_reflected_s']
        with self.assertRaises(KeyError):
            check_common(fixture, self.tol)

    def test_relative_flux_gate_and_weak_reflection_floor(self):
        fixture = analytic_coefficients()
        fixture['quality'].update(total_reflected_s=0.04, flux_decomposition_error_s=0.003)
        checks = {c['name']: c for c in check_common(fixture, self.tol)}
        self.assertEqual(checks['fourier_flux_agreement_s']['status'], 'PASS')
        self.assertEqual(checks['fourier_flux_relative_s']['status'], 'FAIL')
        fixture['quality'].update(total_reflected_s=1e-5, flux_decomposition_error_s=2e-5)
        checks = {c['name']: c for c in check_common(fixture, self.tol)}
        self.assertEqual(checks['fourier_flux_relative_s']['status'], 'PASS')

    def test_missing_runs_are_refused_without_creating_results(self):
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(self.config)
            config['output_dir'] = directory
            report, output = validate_results(config, self.path)
            self.assertEqual(report['status'], 'FAIL')
            self.assertFalse(list(output.rglob('result.npz')))
            self.assertEqual(len(report['cases']), 4)
            self.assertTrue(all(c['status'] == 'FAIL' for c in report['cases']))
            self.assertIn('Missing required artifact', report['cases'][0]['checks'][0]['detail'])
            self.assertEqual(json.loads((output / 'physics_validation.json').read_text(encoding='utf8'))['status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
