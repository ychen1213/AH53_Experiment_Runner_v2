"""Pure comparison math and missing-input refusal; no solver data are created."""
import copy
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tools.compare_backends import compare, compare_reflection, matching_geometry
from ah53runner.planning import load_config

ROOT = Path(__file__).resolve().parents[1]


def mathematical_fixture():
    orders = np.arange(-24, 25)
    electric = np.zeros((49, 2, 2), dtype=complex)
    electric[:, 0, 0] = 0.01 * np.exp(-np.abs(orders)/10)
    electric[:, 1, 1] = 0.02 * np.exp(-np.abs(orders)/10)
    ct = np.sqrt(1 - (orders * 643/16000)**2)
    power = electric * np.sqrt(ct)[:, None, None]
    quality = {'total_reflected_' + pol: float(np.sum(np.abs(power[:, :, j])**2))
               for j, pol in enumerate(('s', 'p'))}
    fdtd = {'result': {'orders': orders, 'r_E': electric.copy(), 'r_power': power.copy()}, 'quality': quality}
    converted = {'r_orders': orders.copy(), 'r_E': electric.copy(), 'r_power': power.copy()}
    return fdtd, converted


class TestBackendComparison(unittest.TestCase):
    def test_equal_in_memory_reflection_passes_and_zero_cross_phase_is_suppressed(self):
        fdtd, converted = mathematical_fixture()
        report, rows = compare_reflection(fdtd, converted)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(len(report['common_orders']), 49)
        self.assertEqual(len(rows), 196)
        self.assertTrue(all(r['phase_difference_deg'] is None for r in rows if r['component'] in ('rsp', 'rps')))

    def test_phase_threshold_is_not_relaxed_and_partial_orders_are_refused(self):
        fdtd, converted = mathematical_fixture()
        fdtd['result']['r_E'] *= np.exp(3j*np.pi/180)
        fdtd['result']['r_power'] *= np.exp(3j*np.pi/180)
        report, _ = compare_reflection(fdtd, converted)
        self.assertEqual(report['status'], 'FAIL')
        self.assertTrue(any(c['name'].startswith('phase_') and c['status'] == 'FAIL' for c in report['checks']))
        converted['r_orders'] = converted['r_orders'][1:]
        with self.assertRaises(ValueError):
            compare_reflection(fdtd, converted)

    def test_numerical_mesh_may_differ_but_geometry_or_material_may_not(self):
        config, _ = load_config(ROOT / 'configs' / 'quick_check.json')
        first = config['base_model']
        second = copy.deepcopy(first)
        second['mesh']['dx_nm'] /= 2
        matching_geometry(first, second)
        second['n_ridge'] += 0.1
        with self.assertRaises(ValueError):
            matching_geometry(first, second)

    def test_missing_real_inputs_are_refused_without_coefficients(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = compare(root / 'missing_fdtd', root / 'missing_rcwa', root / 'missing_certificate.json', root / 'audit')
            self.assertEqual(report['status'], 'REFUSED')
            self.assertFalse(report['evaluated'])
            self.assertFalse((root / 'missing_fdtd').exists())
            self.assertFalse((root / 'missing_rcwa').exists())
            self.assertFalse(list(root.rglob('result.npz')))
            self.assertFalse(list(root.rglob('rcwa_coefficients.npz')))
            self.assertEqual(len((root / 'audit' / 'reflection_comparison.csv').read_text(encoding='utf-8-sig').splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
