"""Offline CLI guards: temporary JSON/text only, no CAD or solver processes."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from ah53runner.rcwa import RCWAError
from tools import run_rcwa


def write_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding='utf8')


class TestRCWAOriginPlanBinding(unittest.TestCase):
    def test_export_uses_same_four_planned_frozen_inputs_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = {'output_dir': 'raw'}
            frozen = [{'case': {'id': f'memory_only_born_{i}'}, 'settings': {'name': 'mesh'},
                       'backend_fingerprint': 'test-only'} for i in range(4)]
            folders = [root/'raw'/item['case']['id']/'mesh' for item in frozen]
            for folder, item in zip(folders, frozen):
                write_state(folder/'input.json', item)
            original = [(folder/'input.json').read_bytes() for folder in folders]
            certificate = {'absolute_origin': {'evidence': [
                {'folder': str(folder.resolve()), 'input_sha256': run_rcwa.rcwa.canonical_hash(item)}
                for folder, item in zip(folders, frozen)]}}
            with patch.object(run_rcwa, 'load_config', return_value=(config, root/'weak.json')), \
                    patch.object(run_rcwa, 'planned_inputs', side_effect=lambda unused: iter(frozen)):
                self.assertEqual(run_rcwa.origin_plan_folders('memory-only', certificate), folders)
                certificate['absolute_origin']['evidence'][0]['input_sha256'] = 'wrong-input'
                with self.assertRaisesRegex(RCWAError, 'differs from the certificate'):
                    run_rcwa.origin_plan_folders('memory-only', certificate)
                write_state(folders[0]/'input.json', dict(frozen[0], backend_fingerprint='stale'))
                with self.assertRaisesRegex(RCWAError, 'differs from --origin-config'):
                    run_rcwa.origin_plan_folders('memory-only')
                # Failed validation only reads; it does not "repair" a stale input.
                self.assertEqual(json.loads((folders[0]/'input.json').read_text())['backend_fingerprint'], 'stale')
            self.assertEqual([(folder/'input.json').read_bytes() for folder in folders[1:]], original[1:])


class TestRCWACLIProcessGuards(unittest.TestCase):
    def test_running_fdtd_state_with_live_worker_refuses_rcwa(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'output_ah53_test' / 'one_case' / 'status.json'
            write_state(state, {'status': 'RUNNING', 'worker_pid': 12345})
            with patch.object(run_rcwa, 'ROOT', root), \
                    patch('run_experiments.recorded_process_alive', return_value=True) as alive:
                with self.assertRaisesRegex(RCWAError, 'FDTD worker/solver is active'):
                    run_rcwa.refuse_running_fdtd()
                alive.assert_called_once_with({'pid':12345,'creation_time_100ns':None})

    def test_finished_fdtd_worker_does_not_block_rcwa(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_state(root / 'output_ah53_test' / 'one_case' / 'status.json',
                        {'status': 'RUNNING', 'worker_pid': 12345})
            with patch.object(run_rcwa, 'ROOT', root), \
                    patch('run_experiments.recorded_process_alive', return_value=False):
                run_rcwa.refuse_running_fdtd()

    def test_nested_rcwa_native_status_with_live_worker_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_state(output / 'case_a' / 'coarse' / 'native_status.json',
                        {'status': 'RUNNING', 'worker_pid': 23456})
            with patch('run_experiments.recorded_process_alive', return_value=True) as alive, \
                    patch.object(run_rcwa, 'windows_process_snapshot') as snapshot:
                with self.assertRaisesRegex(RCWAError, 'RCWA worker remains active'):
                    run_rcwa.refuse_active_workers(output)
                alive.assert_called_once_with({'pid':23456,'creation_time_100ns':None})
                snapshot.assert_not_called()

    def test_dead_worker_with_same_tracked_cad_identity_is_refused(self):
        cad = {'ProcessId': 34567, 'ParentProcessId': 23456,
               'Name': 'fdtd-solutions.exe', 'Created': '2026-10-23T00:00:00.0000000Z'}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_state(output / 'case_a' / 'refined' / 'native_status.json',
                        {'status': 'RUNNING', 'worker_pid': 23456,
                         'owned_cad_processes': [cad]})
            # Substitute only this module's os view; pathlib keeps the host's
            # native path implementation when this test runs outside Windows.
            with patch('run_experiments.recorded_process_alive', return_value=False), \
                    patch.object(run_rcwa, 'os', SimpleNamespace(name='nt')), \
                    patch.object(run_rcwa, 'windows_process_snapshot', return_value=[cad]):
                with self.assertRaisesRegex(RCWAError, 'Owned CAD/solver'):
                    run_rcwa.refuse_active_workers(output)

    def test_reused_cad_pid_with_different_creation_time_is_allowed(self):
        tracked = {'ProcessId': 34567, 'ParentProcessId': 23456,
                   'Name': 'fdtd-solutions.exe', 'Created': '2026-10-23T00:00:00.0000000Z'}
        reused = dict(tracked, Created='2026-10-23T01:00:00.0000000Z')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_state(output / 'case_a' / 'refined' / 'native_status.json',
                        {'status': 'RUNNING', 'worker_pid': 23456,
                         'owned_cad_processes': [tracked]})
            with patch('run_experiments.recorded_process_alive', return_value=False), \
                    patch.object(run_rcwa, 'os', SimpleNamespace(name='nt')), \
                    patch.object(run_rcwa, 'windows_process_snapshot', return_value=[reused]):
                run_rcwa.refuse_active_workers(output)

    def test_raw_collected_with_completed_cleanup_is_not_treated_as_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_state(output / 'case_a' / 'coarse' / 'native_status.json',
                        {'status': 'RAW_COLLECTED', 'worker_pid': 23456})
            with patch('run_experiments.recorded_process_alive', return_value=True) as alive:
                run_rcwa.refuse_active_workers(output)
                alive.assert_not_called()

    def test_running_state_without_tracked_cleanup_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            write_state(output/'case_a'/'coarse'/'native_status.json',{'status':'RUNNING','worker_pid':23456})
            with patch('run_experiments.recorded_process_alive',return_value=False):
                with self.assertRaisesRegex(RCWAError,'no provable CAD cleanup'):
                    run_rcwa.refuse_active_workers(output)

    def test_completed_case_stale_observations_do_not_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            write_state(output/'case_a'/'coarse'/'native_status.json',
                        {'status':'RAW_COMPLETE_PHASE_UNCALIBRATED','worker_pid':23456,
                         'cleanup_failed':False,'owned_cad_processes':[{'ProcessId':34567,'Created':'2026-10-23T00:00:00.0000000Z'}]})
            with patch('run_experiments.recorded_process_alive',return_value=True) as alive, \
                    patch.object(run_rcwa,'windows_process_snapshot') as snapshot:
                run_rcwa.refuse_active_workers(output)
                alive.assert_not_called()
                snapshot.assert_not_called()

    def test_parent_pid_reuse_cannot_attach_an_older_foreign_subtree(self):
        def row(pid,parent,time,name='python.exe'):
            return {'ProcessId':pid,'ParentProcessId':parent,'Created':time,'Name':name}
        times=[f'2026-10-23T00:00:0{i}.0000000Z' for i in range(6)]
        snapshot=[row(100,1,times[2]),
                  row(101,100,times[1],'fdtd-solutions.exe'),
                  row(102,101,times[4],'rcwa-engine.exe'),
                  row(200,100,times[3]),row(201,200,times[4],'fdtd-solutions.exe'),
                  row(300,100,None,'fdtd-solutions.exe')]
        created={item['ProcessId']:run_rcwa.created_ticks(item['Created']) for item in snapshot}
        def identity(pid):
            return {'pid':pid,'creation_time_100ns':created[pid]+1} if created[pid] is not None else None
        with patch.object(run_rcwa,'windows_process_snapshot',return_value=snapshot), \
                patch('run_experiments.process_identity',side_effect=identity):
            owned=run_rcwa.owned_cad_snapshot(100)
            self.assertEqual([item['ProcessId'] for item in owned],[201])
            self.assertEqual(owned[0]['identity']['creation_time_100ns'],created[201]+1)
            self.assertEqual(owned[0]['identity']['parent_pid'],200)
            self.assertEqual(owned[0]['identity']['ownership_root_pid'],100)
            self.assertEqual([item['pid'] for item in owned[0]['lineage']],[201,200,100])

    def test_creation_comparison_preserves_hundred_nanosecond_digit(self):
        first=run_rcwa.created_ticks('2026-10-23T00:00:00.0000001Z')
        second=run_rcwa.created_ticks('2026-10-23T00:00:00.0000002Z')
        self.assertEqual(second-first,1)
        self.assertEqual(first,run_rcwa.created_ticks('2026-10-23T01:00:00.0000001+01:00'))
        self.assertIsNone(run_rcwa.created_ticks(None))
        self.assertIsNone(run_rcwa.created_ticks('not-a-time'))

    def test_cleanup_native_identity_ignores_reused_cad_pid(self):
        identity={'pid':34567,'creation_time_100ns':100}
        with patch('run_experiments.process_alive',return_value=True), \
                patch('run_experiments.process_identity',return_value={'pid':34567,'creation_time_100ns':200}), \
                patch.object(run_rcwa,'windows_process_snapshot') as snapshot:
            self.assertEqual(run_rcwa.active_owned_cads([{'ProcessId':34567,'identity':identity}]),[])
            snapshot.assert_not_called()

    def test_reused_worker_pid_is_not_mistaken_for_the_recorded_worker(self):
        tracked={'ProcessId':34567,'Created':'2026-10-23T00:00:00.0000000Z','Name':'fdtd-solutions.exe'}
        with tempfile.TemporaryDirectory() as temporary:
            output=Path(temporary)
            write_state(output/'case_a'/'coarse'/'native_status.json',
                        {'status':'RUNNING','worker_pid':23456,'worker_creation_time_100ns':100,
                         'owned_cad_processes':[tracked]})
            with patch('run_experiments.process_alive',return_value=True), \
                    patch('run_experiments.process_identity',return_value={'pid':23456,'creation_time_100ns':200}), \
                    patch.object(run_rcwa,'os',SimpleNamespace(name='nt')), \
                    patch.object(run_rcwa,'windows_process_snapshot',return_value=[]):
                run_rcwa.refuse_active_workers(output)

    def test_causal_edge_uses_native_precision_inside_one_cim_microsecond(self):
        time='2026-10-23T00:00:00.0000000Z'
        base=run_rcwa.created_ticks(time)
        rows=[{'ProcessId':100,'ParentProcessId':1,'Created':time,'Name':'python.exe'},
              {'ProcessId':101,'ParentProcessId':100,'Created':time,'Name':'fdtd-solutions.exe'},
              {'ProcessId':102,'ParentProcessId':100,'Created':time,'Name':'fdtd-solutions.exe'}]
        identities={100:base+9,101:base+1,102:base+9}
        with patch.object(run_rcwa,'windows_process_snapshot',return_value=rows), \
                patch('run_experiments.process_identity',side_effect=lambda pid:{'pid':pid,'creation_time_100ns':identities[pid]}):
            self.assertEqual([item['ProcessId'] for item in run_rcwa.owned_cad_snapshot(100)],[102])


class TestRCWACLIAcquisitionFailure(unittest.TestCase):
    def test_recorded_lineage_integrates_with_real_runner_safe_kill_contract(self):
        import run_experiments as runner
        time='2026-10-23T00:00:00.0000000Z'
        start=run_rcwa.created_ticks(time)+1
        rows=[{'ProcessId':100,'ParentProcessId':1,'Created':time,'Name':'python.exe'},
              {'ProcessId':200,'ParentProcessId':100,'Created':'2026-10-23T00:00:01.0000000Z','Name':'launcher.exe'},
              {'ProcessId':300,'ParentProcessId':200,'Created':'2026-10-23T00:00:02.0000000Z','Name':'fdtd-solutions.exe'},
              {'ProcessId':400,'ParentProcessId':100,'Created':'2026-10-22T23:59:59.0000000Z','Name':'fdtd-solutions.exe'}]
        times={row['ProcessId']:run_rcwa.created_ticks(row['Created'])+1 for row in rows}
        with patch.object(run_rcwa,'windows_process_snapshot',return_value=rows), \
                patch.object(runner,'process_identity',side_effect=lambda pid:{'pid':pid,'creation_time_100ns':times[pid]}):
            cads=run_rcwa.owned_cad_snapshot(100)
        self.assertEqual([cad['ProcessId'] for cad in cads],[300])
        observed=[record for cad in cads for record in cad['lineage']]
        root={'pid':100,'creation_time_100ns':start}
        owned=runner.causal_process_tree(root,{record['pid']:record for record in observed})
        self.assertEqual(set(owned),{100,200,300})
        class FakePopen:
            pid=100
            stopped=False
            def poll(self):return 0 if self.stopped else None
            def kill(self):self.stopped=True
            def wait(self,timeout):return 0
        process=FakePopen()
        with patch.object(runner,'os',SimpleNamespace(name='nt')), \
                patch.object(runner,'windows_process_snapshot',return_value={}), \
                patch.object(runner,'terminate_verified_process',return_value=True) as terminate:
            self.assertTrue(runner.kill_process_tree(process,observed_processes=observed,root_identity=root))
        self.assertTrue(process.stopped)
        self.assertEqual([call.args[0]['pid'] for call in terminate.call_args_list],[300,200])

    def test_timeout_passes_start_identity_and_verified_lineage_to_safe_kill(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            (folder/'input.json').write_text('{}\n',encoding='utf8')
            (folder/'build_run_export.lsf').write_text('# Offline timeout fixture.\n',encoding='utf8')
            root={'pid':12345,'creation_time_100ns':100}
            child={'pid':23456,'creation_time_100ns':200,'parent_pid':12345,
                   'parent_creation_time_100ns':100,'ownership_root_pid':12345,
                   'ownership_root_creation_time_100ns':100}
            def time_out(timeout):
                state=json.loads((folder/'native_status.json').read_text(encoding='utf8'))
                state['owned_cad_processes']=[{'ProcessId':23456,'identity':child,'lineage':[child,root]}]
                write_state(folder/'native_status.json',state)
                raise run_rcwa.subprocess.TimeoutExpired('offline-no-process',timeout)
            process=SimpleNamespace(pid=12345,poll=lambda:None,wait=time_out)
            with patch.object(run_rcwa.subprocess,'Popen',return_value=process), \
                    patch('run_experiments.process_identity',return_value=root), \
                    patch('run_experiments.kill_process_tree',return_value=True) as kill:
                self.assertEqual(run_rcwa.acquire(folder,1),124)
            self.assertIs(kill.call_args.args[0],process)
            self.assertEqual(kill.call_args.kwargs['root_identity'],root)
            self.assertIn(child,kill.call_args.kwargs['observed_processes'])
            state=json.loads((folder/'native_status.json').read_text(encoding='utf8'))
            self.assertEqual(state['status'],'FAILED')
            self.assertEqual(state['returncode'],124)
            self.assertFalse(list(folder.rglob('*.fsp')))
            self.assertFalse(list(folder.rglob('*.npz')))

    def test_worker_spawn_error_records_failed_status_without_coefficients(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            # Only the parent spawn/failure path is exercised. The worker never
            # reads these deliberately minimal text fixtures.
            (folder / 'input.json').write_text('{}\n', encoding='utf8')
            (folder / 'build_run_export.lsf').write_text('# Offline spawn-failure fixture only.\n', encoding='utf8')
            with patch.object(run_rcwa.subprocess, 'Popen', side_effect=OSError('mocked spawn failure')) as spawn, \
                    patch('run_experiments.kill_process_tree') as kill:
                code = run_rcwa.acquire(folder, timeout_seconds=1)
                spawn.assert_called_once()
                kill.assert_not_called()
            self.assertEqual(code, 1)
            state = json.loads((folder / 'native_status.json').read_text(encoding='utf8'))
            self.assertEqual(state['status'], 'FAILED')
            self.assertEqual(state['returncode'], 1)
            self.assertIn('Worker spawn failed', state['error'])
            self.assertIn('mocked spawn failure', (folder / 'worker.log').read_text(encoding='utf8'))
            self.assertFalse(list(folder.rglob('*.fsp')))
            self.assertFalse(list(folder.rglob('*.npz')))
            self.assertFalse(list(folder.rglob('rcwa_coefficients*')))


class TestRCWANativeIndexTransfer(unittest.TestCase):
    """Packed in-memory index fixtures, never a saved simulation or result."""
    def fixture(self):
        return {'x':np.arange(800,dtype=float).reshape(-1,1),
                'y':np.array([[0.]]),'z':np.arange(5,dtype=float).reshape(-1,1),
                'lambda':np.array([[643e-9]]),'f':np.array([[299792458./643e-9]]),
                'index_z':np.arange(3200,dtype=float).reshape(3200,1,1),
                'Lumerical_dataset':{'geometry':'rectilinear','attributes':['index_z'],
                                     'parameters':[['lambda','f']]}}

    def translator(self,events):
        class Translator:
            @staticmethod
            def applyLumDatasetConventions(dataset):
                geometry=dataset.get('Lumerical_dataset',{}).get('geometry')
                events.append(geometry)
                if geometry=='rectilinear':
                    raise ValueError('Known point-vs-layer reshape mismatch.')
        return Translator

    def test_only_rectilinear_index_skips_shape_interpretation_and_preserves_values(self):
        raw=self.fixture()
        before=raw['index_z'].copy()
        events=[]
        translator=self.translator(events)
        descriptor=vars(translator)['applyLumDatasetConventions']
        matrix={'Lumerical_dataset':{'attributes':[]}}
        class Session:
            def getresult(self,solver,name):
                self_requested=(solver,name)
                self_assert=self_requested==('RCWA','index')
                if not self_assert:raise AssertionError(self_requested)
                translator.applyLumDatasetConventions(matrix)
                translator.applyLumDatasetConventions(raw)
                return raw
        result,description=run_rcwa.read_packed_native_index(Session(),SimpleNamespace(GetTranslator=translator))
        self.assertIs(result,raw)
        np.testing.assert_array_equal(result['index_z'],before)
        self.assertEqual(result['index_z'].shape,(3200,1,1))
        self.assertNotIn('index_x',result)
        self.assertEqual(description['z_interface_count'],5)
        self.assertEqual(description['z_layer_interval_count'],4)
        self.assertEqual(description['native_attribute_shapes'],{'index_z':[3200,1,1]})
        self.assertFalse(description['attribute_reshape_applied'])
        self.assertFalse(description['interpolation_or_padding_applied'])
        self.assertEqual(events,[None])  # The matrix still used the original translator.
        self.assertIs(vars(translator)['applyLumDatasetConventions'],descriptor)
        translator.applyLumDatasetConventions(matrix)
        self.assertEqual(events,[None,None])
        with self.assertRaisesRegex(ValueError,'Known point-vs-layer'):
            translator.applyLumDatasetConventions(raw)

    def test_api_exception_restores_the_exact_original_descriptor(self):
        events=[]
        translator=self.translator(events)
        descriptor=vars(translator)['applyLumDatasetConventions']
        class Session:
            def getresult(self,solver,name):
                raise OSError('Actual transfer failed.')
        with self.assertRaisesRegex(OSError,'Actual transfer failed'):
            run_rcwa.read_packed_native_index(Session(),SimpleNamespace(GetTranslator=translator))
        self.assertIs(vars(translator)['applyLumDatasetConventions'],descriptor)
        with self.assertRaises(ValueError):
            translator.applyLumDatasetConventions(self.fixture())

    def test_unexpected_packed_size_is_refused_after_restoring_translator(self):
        raw=self.fixture()
        raw['index_z']=raw['index_z'][:-1]
        translator=self.translator([])
        descriptor=vars(translator)['applyLumDatasetConventions']
        class Session:
            def getresult(self,solver,name):
                translator.applyLumDatasetConventions(raw)
                return raw
        with self.assertRaisesRegex(RCWAError,'packed element count'):
            run_rcwa.read_packed_native_index(Session(),SimpleNamespace(GetTranslator=translator))
        self.assertEqual(raw['index_z'].shape,(3199,1,1))
        self.assertIs(vars(translator)['applyLumDatasetConventions'],descriptor)


if __name__ == '__main__':
    unittest.main()
