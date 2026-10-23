"""Independent audit checks using only memory fixtures and absent-output paths."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ah53runner.planning import load_config
from tools.audit_outputs import AuditChecks, COMPONENTS, audit_coefficient_rows, audit_outputs

ROOT = Path(__file__).resolve().parents[1]


def row_fixture():
    """Pure mathematical values; never written as solver results."""
    case = {'id': 'memory', 'group': 'group', 'parameter': 'value', 'value': 2,
            'model': {'period_nm': 16000}}
    r = np.array([[[-0.2 + 0.1j, 0], [0, 0.3 - 0.05j]]])
    valid = {'memory': {'case': case, 'result': {'orders': np.array([0]),
                       'r_E': r, 'lambda_nm': np.array([643.])},
                       'metadata': {'cross_terms_source': 'analytic_by_2d_symmetry'}}}
    row = {'case_id': 'memory', 'group': 'group', 'parameter': 'value', 'value': '2',
           'm': '0', 'theta_deg': '0', 'eta_s': str(abs(r[0, 0, 0])**2),
           'eta_p': str(abs(r[0, 1, 1])**2)}
    for name, (a, b) in COMPONENTS.items():
        z = complex(r[0, a, b])
        row.update({name + '_real': str(z.real), name + '_imag': str(z.imag),
                    name + '_abs': str(abs(z)),
                    name + '_phase_deg': str(math.degrees(math.atan2(z.imag, z.real))) if abs(z) > 1e-8 else '',
                    name + '_source': 'fdtd_fields' if a == b else 'analytic_by_2d_symmetry'})
    return row, valid


class TestOutputAudit(unittest.TestCase):
    def test_independent_csv_math_passes(self):
        row, valid = row_fixture()
        checks = AuditChecks()
        summary = audit_coefficient_rows([row], list(row), valid, {}, checks)
        self.assertEqual(checks.error_count, 0, checks.errors)
        self.assertEqual(summary['expected_rows'], 1)
        self.assertEqual(summary['phase_counts']['blank_below_floor'], 2)

    def test_tampered_complex_value_and_source_are_rejected(self):
        row, valid = row_fixture()
        row['rss_imag'] = '0.25'
        row['rsp_source'] = 'fdtd_fields'
        row['rps_phase_deg'] = '0'
        checks = AuditChecks()
        audit_coefficient_rows([row], list(row), valid, {}, checks)
        self.assertEqual(checks.error_count, 3)

    def test_duplicate_rows_and_unknown_cases_are_rejected(self):
        row, valid = row_fixture()
        checks = AuditChecks()
        audit_coefficient_rows([row, dict(row)], list(row), valid, {}, checks)
        self.assertGreaterEqual(checks.error_count, 2)
        row['case_id'] = 'stale'
        checks = AuditChecks()
        audit_coefficient_rows([row], list(row), valid, {}, checks)
        self.assertGreaterEqual(checks.error_count, 2)

    def test_missing_output_is_rejected_and_only_audit_json_is_written(self):
        config, path = load_config(ROOT / 'configs' / 'quick_check.json')
        with tempfile.TemporaryDirectory() as directory:
            config = copy.deepcopy(config)
            output = Path(directory) / 'missing_real_outputs'
            config['output_dir'] = str(output)
            audit_path = Path(directory) / 'audit.json'
            report = audit_outputs(config, path, audit_path, require_all=True)
            self.assertEqual(report['status'], 'FAIL')
            self.assertEqual(report['valid_done_cases'], 0)
            self.assertFalse(output.exists())
            self.assertTrue(audit_path.is_file())
            self.assertEqual(list(Path(directory).iterdir()), [audit_path])


if __name__ == '__main__':
    unittest.main()
