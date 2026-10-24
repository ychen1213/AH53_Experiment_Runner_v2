"""Offline execution-contract checks; the CAD boundary is entirely mocked.

The fake FSP/MAT byte strings are temporary fixtures, never physical evidence.
No Lumerical import, license, subprocess, or real solver is used by these tests.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ah53runner import lsf_runtime, lsf_worker
from ah53runner.lsf_config import inspect_lsf, make_bundle
from run_experiments import archive_previous_attempt, result_valid, should_run_case


class _FakeSession:
    """Expose only the API calls needed to exercise worker state transitions."""

    def __init__(self, outcome='success', native_output=True):
        self.outcome = outcome
        self.native_output = native_output
        self.values = {}
        self.eval_count = 0
        self.run_count = 0
        self.closed = False
        self.folder = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True

    def cd(self, directory):
        self.folder = Path(directory)

    def version(self):
        return 'OFFLINE_FAKE_CAD_NOT_A_REAL_VERSION'

    def putv(self, name, value):
        self.values[name] = value

    def eval(self, command):
        self.eval_count += 1
        source_name = re.search(r'eval\((AH53_source_[a-f0-9]+)\)', command).group(1)
        error_name = re.search(r'catch\((AH53_error_[a-f0-9]+)\)', command).group(1)
        source = self.values[source_name]
        if self.outcome == 'error':
            self.values[error_name] = 'fixture native error: object was not found'
            (self.folder / 'partial.txt').write_text('partial fixture', encoding='utf8')
            return
        if self.outcome == 'early_break':
            return
        marker = re.search(r'(AH53_completed_[a-f0-9]+) = 1;\s*$', source).group(1)
        self.values[marker] = 1.0
        if self.native_output:
            (self.folder / 'native.mat').write_bytes(b'OFFLINE-NATIVE-MAT-FIXTURE')

    def getv(self, name):
        return self.values[name]

    def run(self):
        self.run_count += 1
        raise AssertionError('The worker must never append an extra solver run.')

    def save(self, filename):
        Path(filename).write_bytes(b'OFFLINE-FSP-FIXTURE-NOT-A-LUMERICAL-PROJECT')


class LsfExecutionContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ah53_lsf_offline_')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def prepare(self, name='task', values=(0,), dependency=False, required=True):
        source = 'clear;\nh=300;\nt=0;\ntop=h+t;\naddrect;\nrun;\n'
        parameter = next(p['id'] for p in inspect_lsf(source)['parameters'] if p['label'] == 't')
        bundle = make_bundle(source, 'fixture.lsf', parameter, list(values), name=name)
        bundle['output_dir'] = str(self.root / (name + '_output'))
        # A deliberately nonexistent API path proves the mocked boundary is used.
        bundle['lumerical_api_python'] = str(self.root / 'NO_REAL_LUMERICAL_API')
        bundle['execution'] = {'mode': 'script', 'input_files': [],
                               'required_outputs': ['native.mat'] if required else []}
        if dependency:
            source_file = self.root / (name + '_material.mdf')
            source_file.write_bytes(b'OFFLINE-MDF-INPUT-FIXTURE')
            bundle['execution']['input_files'] = [
                {'source': str(source_file), 'relative_path': 'materials/input.mdf'}]
        config = self.root / (name + '.json')
        config.write_text(json.dumps(bundle), encoding='utf8')
        cfg, path = lsf_runtime.load_lsf_config(config)
        cases = lsf_runtime.plan_lsf(cfg, path)
        output = lsf_runtime.prepare_lsf(path, cfg, cases, archive_previous_attempt)
        return cfg, path, cases, output

    def execute(self, case, folder, outcome='success', native_output=True):
        session = _FakeSession(outcome=outcome, native_output=native_output)
        api = SimpleNamespace(FDTD=lambda **unused: session)
        with patch.object(lsf_worker, '_load_lumapi', return_value=api), \
                contextlib.redirect_stdout(io.StringIO()):
            result = lsf_worker.run_lsf_case(case, folder)
        return result, session

    def done(self, case, folder):
        (folder / 'status.json').write_text(json.dumps({
            'status': 'DONE', 'digest': case['digest'],
            'solver_fingerprint': case['solver_fingerprint']}), encoding='utf8')

    def test_complete_script_is_evaluated_once_and_valid_done_is_skipped(self):
        _, _, cases, output = self.prepare()
        case, folder = cases[0], output / cases[0]['id']
        result, session = self.execute(case, folder)
        self.assertEqual(session.eval_count, 1)
        self.assertEqual(session.run_count, 0)
        self.assertTrue(session.closed)
        self.assertEqual(result['completion_kind'], 'script_completed')
        self.assertNotIn('physics_validated', result)
        self.assertTrue(lsf_runtime.load_lsf_artifacts(folder, case, require_done=False))
        self.assertFalse(result_valid(folder, case))  # Completion requires parent DONE.
        self.done(case, folder)
        self.assertTrue(result_valid(folder, case))
        self.assertFalse(should_run_case(folder, case))
        self.assertFalse(should_run_case(folder, case, retry_failed=True))

    def test_native_error_and_early_break_leave_no_success_record_and_next_case_works(self):
        for outcome, message in [('error', 'Native LSF error'),
                                 ('early_break', 'completion marker')]:
            with self.subTest(outcome=outcome):
                _, _, cases, output = self.prepare(name=outcome, values=(0, 100))
                failed, next_case = cases
                folder = output / failed['id']
                cwd = Path.cwd()
                with self.assertRaisesRegex(RuntimeError, message):
                    self.execute(failed, folder, outcome=outcome)
                self.assertEqual(Path.cwd(), cwd)
                self.assertFalse((folder / 'metadata.json').exists())
                self.assertFalse((folder / 'script_result.json').exists())
                self.assertFalse(result_valid(folder, failed))
                next_folder = output / next_case['id']
                self.execute(next_case, next_folder)
                self.done(next_case, next_folder)
                self.assertTrue(result_valid(next_folder, next_case))
                self.assertFalse((next_folder / 'partial.txt').exists())

    def test_missing_required_native_output_is_not_completion(self):
        _, _, cases, output = self.prepare()
        case, folder = cases[0], output / cases[0]['id']
        with self.assertRaisesRegex(RuntimeError, 'required output: native.mat'):
            self.execute(case, folder, native_output=False)
        self.assertTrue((folder / 'model.fsp').exists())  # Project alone is insufficient.
        self.assertFalse((folder / 'metadata.json').exists())
        self.assertFalse((folder / 'script_result.json').exists())
        self.assertFalse(result_valid(folder, case))

    def test_dependency_change_is_detected_before_api_and_changes_case_identity(self):
        cfg, path, cases, output = self.prepare(dependency=True)
        case, folder = cases[0], output / cases[0]['id']
        dependency = Path(case['execution']['input_files'][0]['source'])
        dependency.write_bytes(b'CHANGED-MATERIAL-INPUT')
        with patch.object(lsf_worker, '_load_lumapi') as load_api:
            with self.assertRaisesRegex(ValueError, 'changed since planning'):
                lsf_worker.run_lsf_case(case, folder)
            load_api.assert_not_called()
        self.assertNotEqual(lsf_runtime.plan_lsf(cfg, path)[0]['digest'], case['digest'])

    def test_output_script_parameter_and_staged_input_tampering_invalidates_resume(self):
        _, _, cases, output = self.prepare(dependency=True)
        case, folder = cases[0], output / cases[0]['id']
        self.execute(case, folder)
        self.done(case, folder)
        self.assertTrue(result_valid(folder, case))
        for relative in ('model.fsp', 'native.mat', 'script_result.json',
                         'script.lsf', 'parameters.json', 'materials/input.mdf'):
            with self.subTest(relative=relative):
                target = folder / relative
                original = target.read_bytes()
                changed = bytes([original[0] ^ 1]) + original[1:]
                target.write_bytes(changed)  # Same length: SHA must detect the edit.
                try:
                    self.assertFalse(result_valid(folder, case))
                    self.assertTrue(should_run_case(folder, case))
                    self.assertTrue(should_run_case(folder, case, retry_failed=True))
                finally:
                    target.write_bytes(original)
        self.assertTrue(result_valid(folder, case))

    def test_failed_status_requires_retry_and_prior_outputs_are_archived(self):
        cfg, path, cases, output = self.prepare()
        case, folder = cases[0], output / cases[0]['id']
        (folder / 'partial.mat').write_bytes(b'PARTIAL-NATIVE-FIXTURE')
        (folder / 'worker.log').write_text('fixture failure', encoding='utf8')
        (folder / 'status.json').write_text(json.dumps({'status': 'FAILED',
            'digest': case['digest'], 'solver_fingerprint': case['solver_fingerprint'],
            'attempt_id': 'offline_failed_attempt'}), encoding='utf8')
        self.assertFalse(should_run_case(folder, case))
        self.assertTrue(should_run_case(folder, case, retry_failed=True))
        archived = archive_previous_attempt(folder)
        self.assertEqual((archived / 'partial.mat').read_bytes(), b'PARTIAL-NATIVE-FIXTURE')
        self.assertTrue((archived / 'script.lsf').exists())
        self.assertTrue((archived / 'parameters.json').exists())
        self.assertTrue((folder / 'script.lsf').exists())
        self.assertFalse((folder / 'partial.mat').exists())
        self.execute(case, folder)
        self.done(case, folder)
        self.assertTrue(result_valid(folder, case))
        self.assertNotIn('attempts', ''.join(item['path'] for item in
            lsf_runtime.load_lsf_artifacts(folder, case)['files']))

    def test_implementation_change_invalidates_resume_and_preserves_old_attempt(self):
        cfg, path, cases, output = self.prepare()
        case, folder = cases[0], output / cases[0]['id']
        self.execute(case, folder)
        self.done(case, folder)
        with patch.object(lsf_runtime, 'lsf_fingerprint', return_value='offline_changed_fingerprint'):
            self.assertFalse(result_valid(folder, case))
            changed = lsf_runtime.plan_lsf(cfg, path)
            self.assertEqual(changed[0]['digest'], case['digest'])
            lsf_runtime.prepare_lsf(path, cfg, changed, archive_previous_attempt)
        state = json.loads((folder / 'status.json').read_text(encoding='utf8'))
        self.assertEqual(state['status'], 'INCOMPLETE')
        archived = folder / state['previous_attempt']
        self.assertTrue((archived / 'model.fsp').is_file())
        self.assertTrue((archived / 'script_result.json').is_file())
        self.assertFalse((folder / 'model.fsp').exists())


if __name__ == '__main__':
    unittest.main()
