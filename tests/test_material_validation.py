"""In-memory acceptance-policy tests; no synthetic solver files are published."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from ah53runner.planning import load_config
from ah53runner.materials import EFFECTIVE_ABSORPTION_K_THRESHOLD, is_effectively_absorbing
from ah53runner.validation import fresnel_at_reference
from tools import validate_materials as gate

ROOT = Path(__file__).resolve().parents[1]


def analytic_memory_artifact(case, n, raw_k=None):
    """Math-only stand-in for policy tests, returned in memory and never saved."""
    model = case['model']
    n = complex(n)
    wavelength = model['wavelength_nm']
    reflected = abs((1-n)/(1+n))**2
    depth = -model['sim']['transmission_y_um']*1000
    transmitted = n.real*abs(2/(1+n))**2*np.exp(-4*np.pi*n.imag*depth/wavelength)
    coefficient = fresnel_at_reference(n, wavelength, model['sim']['reference_y_um'], -1)
    jones = np.eye(2, dtype=complex)[None, :, :]*coefficient
    raw_k = n.imag if raw_k is None else raw_k
    q = {'incident_spatial_phase_sign': -1,
         'absorption_present': is_effectively_absorbing({'k':n.imag,'raw_k':raw_k})}
    for pol in ('s', 'p'):
        q.update({f'total_reflected_{pol}': reflected, f'total_transmitted_{pol}': transmitted,
                  f'power_closure_{pol}': reflected+transmitted,
                  f'energy_residual_{pol}': 1-reflected-transmitted,
                  f'air_reference_reflected_{pol}': 0., f'flux_decomposition_error_{pol}': 0.,
                  f'signed_reflection_flux_{pol}': reflected,
                  f'signed_transmission_flux_{pol}': -transmitted})
    material = {'kind': 'database', 'name': model['substrate_material'],
                'n': n.real, 'k': n.imag, 'raw_n': n.real, 'raw_k': raw_k,
                'definition_sha256': 'TEST_ONLY', 'validation': {'status': 'PASS'}}
    materials = {'roles': {'substrate': material}, 'material_databases': [],
                 'fit_wavelength_range_nm': [wavelength-100, wavelength+100]}
    result = {'orders': np.array([0]), 'theta_deg': np.array([0.]),
              'r_E': jones, 'r_power': jones.copy(), 'eta_s': np.array([reflected]),
              'eta_p': np.array([reflected]), 'lambda_nm': np.array([wavelength]),
              'quality': q, 'materials': materials}
    metadata = {'materials': materials, 'lumerical_version': 'IN_MEMORY_TEST_ONLY',
                'solver_fingerprint': case['solver_fingerprint'], 'result_sha256': 'TEST_ONLY',
                'material_resolution_sha256': 'TEST_ONLY', 'power_monitor_planes': {
                    'reflection_y_um': model['sim']['reflection_y_um'],
                    'transmission_y_um': model['sim']['transmission_y_um']}}
    return result, metadata


class TestMaterialAcceptance(unittest.TestCase):
    def setUp(self):
        config, path = load_config(ROOT/'configs/quick_check.json')
        self.path = path
        self.config = copy.deepcopy(config)
        model = self.config['base_model']
        model.pop('n_substrate')
        model.update(geometry_kind='planar_interface', films=[], substrate_material='Glass test')
        self.config['capture_field_count_per_group'] = 0
        self.config['experiments'] = [
            {'name': 'lossless_label', 'parameter': 'wavelength_nm', 'values': [643]},
            {'name': 'absorbing_label', 'parameter': 'wavelength_nm', 'values': [643],
             'overrides': {'substrate_material': 'Absorber test'}},
        ]

    def loader(self, folder, case):
        n = 1.5 if case['model']['substrate_material'] == 'Glass test' else 1.5+.2j
        return analytic_memory_artifact(case, n)

    def evaluate(self, loader=None):
        with patch.object(gate, 'load_completed_case', side_effect=loader or self.loader), \
             patch.object(gate, 'evidence_hashes', return_value={'test': 'IN_MEMORY_ONLY'}):
            return gate.evaluate_material_cases(self.config, self.path)

    def test_real_index_classes_provide_minimum_passing_coverage(self):
        report = self.evaluate()
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['passed_cases'], 2)
        self.assertTrue(all(item['satisfied'] for item in report['coverage'].values()))
        self.assertIn('ah53runner/validation.py', report['validation_source_sha256'])
        for record in report['cases']:
            self.assertIn('fresnel_internal_plane_transmission_p', {c['name'] for c in record['checks']})
        self.assertNotIn('air_interface', report['coverage'])

    def test_group_names_do_not_fabricate_lossless_coverage(self):
        report = self.evaluate(lambda folder, case: analytic_memory_artifact(case, 1.5+.2j))
        self.assertEqual(report['passed_cases'], 2)
        self.assertEqual(report['status'], 'FAIL')
        self.assertFalse(report['coverage']['named_lossless_planar']['satisfied'])
        self.assertEqual(len(report['coverage']['named_absorbing_planar']['passed_cases']), 2)

    def test_numerical_fit_loss_coverage_keeps_actual_k_and_raw_weak_absorption(self):
        def trace_fit(folder,case):
            if case['model']['substrate_material']=='Glass test':
                return analytic_memory_artifact(case,1.45667520+1.52995e-10j,raw_k=0.)
            return self.loader(folder,case)
        report=self.evaluate(trace_fit)
        self.assertEqual(report['status'],'PASS')
        self.assertEqual(report['classification_k_threshold'],EFFECTIVE_ABSORPTION_K_THRESHOLD)
        self.assertEqual(report['cases'][0]['substrate']['k'],1.52995e-10)
        self.assertEqual(report['cases'][0]['material_class'],'named_lossless_planar')
        # The same small k supplied by the real material is not fit roundoff.
        genuine=self.evaluate(lambda folder,case:analytic_memory_artifact(case,1.5+1.52995e-10j))
        self.assertEqual(genuine['status'],'FAIL')
        self.assertFalse(genuine['coverage']['named_lossless_planar']['satisfied'])

    def test_zero_loss_fit_cannot_claim_actual_absorbing_coverage(self):
        def erased_loss(folder,case):
            if case['model']['substrate_material']=='Absorber test':
                return analytic_memory_artifact(case,1.5,raw_k=1e-10)
            return self.loader(folder,case)
        report=self.evaluate(erased_loss)
        self.assertEqual(report['status'],'FAIL')
        self.assertFalse(report['coverage']['named_absorbing_planar']['satisfied'])
        self.assertTrue(any(item['name']=='absorbing_fit_retains_loss'
                            for item in report['cases'][1]['checks']))

    def test_failing_transmission_does_not_count_as_passing_coverage(self):
        def wrong_t(folder, case):
            result, metadata = self.loader(folder, case)
            if case['model']['substrate_material'] == 'Glass test':
                result['quality']['total_transmitted_s'] = .949984
                result['quality']['power_closure_s'] = .04+.949984
            return result, metadata
        report = self.evaluate(wrong_t)
        self.assertEqual(report['status'], 'FAIL')
        coverage = report['coverage']['named_lossless_planar']
        self.assertEqual(len(coverage['evaluated_cases']), 1)
        self.assertEqual(coverage['passed_cases'], [])
        failed = report['cases'][0]
        self.assertTrue(any(c['name'] == 'fresnel_internal_plane_transmission_s'
                            and c['status'] == 'FAIL' for c in failed['checks']))

    def test_stale_fingerprint_does_not_provide_coverage(self):
        def stale(folder, case):
            result, metadata = self.loader(folder, case)
            metadata['solver_fingerprint'] = 'STALE'
            return result, metadata
        report = self.evaluate(stale)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['passed_cases'], 0)
        self.assertTrue(all(not item['evaluated_cases'] for item in report['coverage'].values()))

    def test_named_gate_requires_independent_internal_t_checks(self):
        with patch.object(gate, 'check_planar', return_value=[]):
            report = self.evaluate()
        self.assertEqual(report['status'], 'FAIL')
        self.assertTrue(all(any(c['name'] == 'independent_internal_plane_T_checks'
                                and c['status'] == 'FAIL' for c in r['checks']) for r in report['cases']))

    def test_nonplanar_case_is_not_mislabeled_as_planar(self):
        self.config['experiments'][1]['overrides']['geometry_kind'] = 'ah53'
        report = self.evaluate()
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('requires geometry_kind=planar_interface', report['cases'][1]['checks'][0]['detail'])

    def test_missing_real_runs_write_only_failure_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            self.config['output_dir'] = directory
            report, output = gate.validate_material_results(self.config, self.path)
            self.assertEqual(report['status'], 'FAIL')
            self.assertEqual(report['passed_cases'], 0)
            self.assertEqual({p.name for p in output.iterdir()},
                             {gate.REPORT_STEM+'.json', gate.REPORT_STEM+'.md'})
            self.assertFalse(list(output.rglob('result.npz')))
            self.assertIn('结论：FAIL', (output/(gate.REPORT_STEM+'.md')).read_text(encoding='utf8'))

    def test_unknown_or_invalid_tolerances_are_refused(self):
        for settings in ({'fresnel_power_absolute': -1}, {'typo_tolerance': 1},
                         {'phase_deg': float('nan')}, {'phase_deg': True}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                config = dict(self.config, validation_tolerances=settings)
                gate.evaluate_material_cases(config, self.path)

    def test_missing_native_evidence_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'Missing/empty actual solver evidence'):
                gate.evidence_hashes(directory)

    def test_cli_missing_runs_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            self.config['output_dir'] = directory
            with patch.object(gate, 'load_config', return_value=(self.config, self.path)):
                self.assertEqual(gate.main(['--config', str(self.path)]), 2)


if __name__ == '__main__':
    unittest.main()
