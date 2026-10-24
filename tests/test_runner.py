"""Temporary mathematical fixtures only; these are never FDTD validation data."""
import copy
import csv
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import numpy as np

from ah53runner.artifacts import ArtifactError, file_sha256, load_artifacts, solver_fingerprint
from ah53runner.report import _phase_pair, _strict_power_warnings, make_report
from run_experiments import (QueueLock, archive_previous_attempt, dump_json,
                             execute_command, launch_case, main, prepare, process_identity,
                             result_valid, should_run_case, causal_process_tree,
                             recorded_process_alive, state_process_records,
                             observed_descendants_exited, kill_process_tree)


class TestRunnerIntegrity(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='ah53_test_fixture_')
        self.root=Path(self.temporary.name)
        self.case={'id':'fixture_000','digest':'test-fixture-only','group':'fixture',
                   'parameter':'ridge_height_nm','value':100,
                   'solver_fingerprint':solver_fingerprint(),'capture_field':False,
                   'model':{'period_nm':1000,'wavelength_nm':600}}
        self.folder=self.root/self.case['id']
        self.folder.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def write_fixture(self,case=None):
        case=case or self.case
        folder=self.root/case['id']
        folder.mkdir(exist_ok=True)
        orders=np.array([-1,0,1])
        theta=np.rad2deg(np.arcsin(orders*.6))
        cosine=np.cos(np.deg2rad(theta))
        jones=np.zeros((3,2,2),dtype=complex)
        jones[:,0,0]=.1+.02j
        jones[:,1,1]=.2+.04j
        eta_s=cosine*np.abs(jones[:,0,0])**2
        eta_p=cosine*np.abs(jones[:,1,1])**2
        quality={'selected_lambda_nm':600,'incident_spatial_phase_sign':-1,
                 'nearfield_warnings':[]}
        for pol,eta in (('s',eta_s),('p',eta_p)):
            quality.update({'total_reflected_'+pol:float(sum(eta)),
                            'total_transmitted_'+pol:float(1-sum(eta)),
                            'sum_eta_'+pol:float(sum(eta)),
                            'flux_decomposition_error_'+pol:0.,'power_closure_'+pol:1.})
        values={'orders':orders,'theta_deg':theta,'r_E':jones,
                'r_power':jones*np.sqrt(cosine)[:,None,None],
                'eta_s':eta_s,'eta_p':eta_p,'lambda_nm':np.array([600.]),
                'quality_json':np.array([json.dumps(quality)])}
        np.savez_compressed(folder/'result.npz',**values)
        metadata={'case_id':case['id'],'digest':case['digest'],
                  'solver_fingerprint':case['solver_fingerprint'],
                  'result_sha256':file_sha256(folder/'result.npz'),'quality':quality,
                  'jones_phase_status':'TEST FIXTURE; NOT FDTD',
                  'cross_terms_source':'analytic_by_2d_symmetry'}
        dump_json(folder/'physics_metadata.json',metadata)
        dump_json(folder/'case.json',case)
        dump_json(folder/'status.json',{'status':'DONE','digest':case['digest'],
                  'solver_fingerprint':case['solver_fingerprint'],'attempt_id':'fixture_attempt'})
        (folder/'AH53_model.fsp').write_bytes(b'TEST FIXTURE; NOT A LUMERICAL MODEL')
        (folder/'worker.log').write_text('TEST FIXTURE ONLY\n',encoding='utf8')
        if case.get('capture_field'):
            for pol in ('s','p'):
                np.savez_compressed(folder/f'nearfield_{pol}.npz',x_um=np.array([-1.,1.]),
                                    y_um=np.array([0.,1.]),E2=np.ones((2,2)))
        return values,metadata

    def rewrite_fixture(self,values,metadata):
        np.savez_compressed(self.folder/'result.npz',**values)
        metadata['result_sha256']=file_sha256(self.folder/'result.npz')
        dump_json(self.folder/'physics_metadata.json',metadata)

    def test_complete_and_damaged_required_arrays(self):
        values,metadata=self.write_fixture()
        self.assertTrue(result_valid(self.folder,self.case))
        for key in ('theta_deg','eta_s','eta_p','r_power','quality_json','lambda_nm'):
            with self.subTest(missing=key):
                damaged=dict(values)
                del damaged[key]
                self.rewrite_fixture(damaged,metadata)
                self.assertFalse(result_valid(self.folder,self.case))

    def test_rejects_nonfinite_inconsistent_and_wrong_wavelength(self):
        values,metadata=self.write_fixture()
        for key,replacement in (('eta_s',np.array([np.nan,0.,0.])),
                                ('eta_p',np.ones(3)),('lambda_nm',np.array([608.]))):
            with self.subTest(key=key):
                damaged=dict(values);damaged[key]=replacement
                self.rewrite_fixture(damaged,metadata)
                with self.assertRaises(ArtifactError):load_artifacts(self.folder,self.case)

    def test_rejects_digest_runtime_and_checksum_mismatch(self):
        _,metadata=self.write_fixture()
        for key in ('digest','solver_fingerprint','result_sha256'):
            with self.subTest(key=key):
                damaged=dict(metadata);damaged[key]='wrong'
                dump_json(self.folder/'physics_metadata.json',damaged)
                self.assertFalse(result_valid(self.folder,self.case))

    def test_rejects_changed_frozen_input(self):
        self.write_fixture()
        frozen=copy.deepcopy(self.case)
        frozen['model']['period_nm']=2000
        dump_json(self.folder/'case.json',frozen)
        self.assertFalse(result_valid(self.folder,self.case))

    def test_prepare_archives_changed_input_before_replacing_it(self):
        self.write_fixture()
        altered=copy.deepcopy(self.case);altered['model']['period_nm']=2000
        dump_json(self.folder/'case.json',altered)
        prepare(self.root/'config.json',{'output_dir':str(self.root)},[self.case],preview=False)
        archived=next((self.folder/'attempts').iterdir())
        saved=json.loads((archived/'case.json').read_text(encoding='utf8'))
        self.assertEqual(saved['model']['period_nm'],2000)
        self.assertEqual(json.loads((self.folder/'status.json').read_text(encoding='utf8'))['status'],'INCOMPLETE')

    def test_nonfinite_json_is_rejected_on_read_and_write(self):
        _,metadata=self.write_fixture()
        metadata['unrelated_invalid_number']=float('nan')
        (self.folder/'physics_metadata.json').write_text(json.dumps(metadata),encoding='utf8')
        self.assertFalse(result_valid(self.folder,self.case))
        with self.assertRaises(ValueError):dump_json(self.root/'invalid.json',{'value':float('inf')})

    def test_requested_nearfields_required_and_validated(self):
        self.case['capture_field']=True
        self.write_fixture()
        self.assertTrue(result_valid(self.folder,self.case))
        (self.folder/'nearfield_p.npz').unlink()
        self.assertFalse(result_valid(self.folder,self.case))
        np.savez_compressed(self.folder/'nearfield_p.npz',x_um=[-1.,1.],y_um=[0.,1.],
                            E2=np.array([[1.,np.nan],[1.,1.]]))
        self.assertFalse(result_valid(self.folder,self.case))

    def test_retry_failed_excludes_pending_and_valid_done(self):
        self.assertFalse(should_run_case(self.folder,self.case,retry_failed=True))
        self.assertTrue(should_run_case(self.folder,self.case))
        dump_json(self.folder/'status.json',{'status':'FAILED'})
        self.assertTrue(should_run_case(self.folder,self.case,retry_failed=True))
        self.assertFalse(should_run_case(self.folder,self.case))
        self.write_fixture()
        self.assertFalse(should_run_case(self.folder,self.case,retry_failed=True))
        (self.folder/'result.npz').write_bytes(b'broken archive')
        self.assertTrue(should_run_case(self.folder,self.case,retry_failed=True))
        self.assertTrue(should_run_case(self.folder,self.case))

    def test_archives_partial_models_logs_and_raw_subdirectories(self):
        self.write_fixture()
        (self.folder/'raw').mkdir()
        (self.folder/'raw'/'partial.txt').write_text('partial solver output',encoding='utf8')
        archived=archive_previous_attempt(self.folder)
        self.assertTrue((archived/'AH53_model.fsp').exists())
        self.assertTrue((archived/'worker.log').exists())
        self.assertTrue((archived/'raw'/'partial.txt').exists())
        self.assertTrue((archived/'case.json').exists())
        self.assertTrue((self.folder/'case.json').exists())
        self.assertFalse((self.folder/'result.npz').exists())

    def test_zero_exit_with_incomplete_artifacts_is_failed(self):
        dump_json(self.folder/'case.json',self.case)
        process=Mock(pid=123456,returncode=0)
        process.wait.return_value=0
        process.poll.return_value=0
        with patch('run_experiments.subprocess.Popen',return_value=process):
            success,_=launch_case(self.root,self.case,'',60,False)
        self.assertFalse(success)
        status=json.loads((self.folder/'status.json').read_text(encoding='utf8'))
        self.assertEqual(status['status'],'FAILED')
        self.assertIn('artifact validation',status['error'])

    def test_interrupt_cleans_owned_worker_and_records_failure(self):
        dump_json(self.folder/'case.json',self.case)
        process=Mock(pid=123456)
        process.wait.side_effect=KeyboardInterrupt
        process.poll.return_value=None
        with patch('run_experiments.subprocess.Popen',return_value=process), \
             patch('run_experiments.kill_process_tree',return_value=True) as cleanup:
            with self.assertRaises(KeyboardInterrupt):
                launch_case(self.root,self.case,'',60,False)
        cleanup.assert_called_once()
        self.assertIs(cleanup.call_args.args[0],process)
        status=json.loads((self.folder/'status.json').read_text(encoding='utf8'))
        self.assertEqual(status['status'],'FAILED')
        self.assertTrue(status['interrupted'])

    def test_spawn_failure_is_recorded_and_returns_to_queue(self):
        dump_json(self.folder/'case.json',self.case)
        with patch('run_experiments.subprocess.Popen',side_effect=OSError('test launch error')):
            success,_=launch_case(self.root,self.case,'',60,False)
        self.assertFalse(success)
        status=json.loads((self.folder/'status.json').read_text(encoding='utf8'))
        self.assertEqual(status['status'],'FAILED')
        self.assertIn('test launch error',status['error'])

    def test_timeout_stops_owned_worker_and_records_failure(self):
        dump_json(self.folder/'case.json',self.case)
        process=Mock(pid=123456)
        process.poll.return_value=None
        with patch('run_experiments.subprocess.Popen',return_value=process), \
             patch('run_experiments.wait_with_memory_samples',side_effect=subprocess.TimeoutExpired('fixture',60)), \
             patch('run_experiments.kill_process_tree',return_value=True) as cleanup:
            success,_=launch_case(self.root,self.case,'',60,False)
        cleanup.assert_called_once()
        self.assertIs(cleanup.call_args.args[0],process)
        self.assertFalse(success)
        status=json.loads((self.folder/'status.json').read_text(encoding='utf8'))
        self.assertEqual(status['returncode'],124)
        self.assertTrue(status['timeout'])

    def test_queue_lock_reentry_and_orphan_worker(self):
        with QueueLock(self.root):
            with self.assertRaises(RuntimeError):
                with QueueLock(self.root):pass
        with QueueLock(self.root):pass
        identity=process_identity(os.getpid())
        dump_json(self.folder/'status.json',{'status':'RUNNING','worker_pid':os.getpid(),
                                            'worker_creation_time_100ns':identity['creation_time_100ns']})
        with self.assertRaises(RuntimeError):
            with QueueLock(self.root):pass

    def test_orphan_solver_identity_is_checked_after_worker_exits(self):
        identity=process_identity(os.getpid())
        dump_json(self.folder/'status.json',{'status':'RUNNING','worker_pid':123456,
                                            'worker_creation_time_100ns':1,
                                            'observed_processes':[identity]})
        with self.assertRaises(RuntimeError):
            with QueueLock(self.root):pass

    def test_pid_reuse_cannot_attach_an_older_qa_server_to_new_worker(self):
        worker={'pid':48856,'creation_time_100ns':134371598539314040,'parent_pid':1}
        own_child={'pid':53780,'creation_time_100ns':134371598539514040,'parent_pid':48856}
        qa={'pid':54472,'creation_time_100ns':134371597580000000,'parent_pid':53780}
        old_other={'pid':49976,'creation_time_100ns':134371597580000001,'parent_pid':54472}
        own_solver={'pid':60000,'creation_time_100ns':134371598539714040,'parent_pid':53780}
        records={r['pid']:r for r in (worker,own_child,qa,old_other,own_solver)}
        owned=causal_process_tree(worker,records)
        self.assertEqual(set(owned),{48856,53780,60000})
        state={'worker_pid':48856,'worker_creation_time_100ns':worker['creation_time_100ns'],
               'observed_processes':[qa,old_other]}
        self.assertEqual([r['pid'] for r in state_process_records(state)],[48856])
        self.assertTrue(observed_descendants_exited(state))

    def test_unknown_or_access_denied_pid_is_never_confirmed_owned(self):
        record={'pid':57064,'creation_time_100ns':134371598000000000}
        with patch('run_experiments.process_identity',return_value=None), \
             patch('run_experiments.process_alive',return_value=True):
            self.assertFalse(recorded_process_alive(record))
            dump_json(self.folder/'status.json',{'status':'FAILED','cleanup_failed':True,
                       'worker_pid':48856,'worker_creation_time_100ns':134371597000000000,
                       'observed_processes':[record]})
            with QueueLock(self.root):pass
        with patch('run_experiments.process_identity',return_value={'pid':57064,'creation_time_100ns':134371599000000000}), \
             patch('run_experiments.process_alive',return_value=True):
            self.assertFalse(recorded_process_alive(record))

    def test_done_history_never_blocks_on_its_old_observations(self):
        dump_json(self.folder/'status.json',{'status':'DONE','cleanup_failed':False,
                   'worker_pid':48856,'worker_creation_time_100ns':10,
                   'observed_processes':[{'pid':57064,'creation_time_100ns':20}]})
        with patch('run_experiments.recorded_process_alive',return_value=True) as check:
            with QueueLock(self.root):pass
        check.assert_not_called()

    def test_cleanup_terminates_only_verified_time_causal_descendants(self):
        worker={'pid':100,'creation_time_100ns':1000,'parent_pid':1}
        child={'pid':200,'creation_time_100ns':1100,'parent_pid':100}
        unrelated={'pid':300,'creation_time_100ns':500,'parent_pid':200}
        process=Mock(pid=100)
        process.poll.side_effect=[None,0]
        with patch('run_experiments.windows_process_snapshot',return_value={100:worker,200:child,300:unrelated}), \
             patch('run_experiments.terminate_verified_process',return_value=True) as terminate:
            self.assertTrue(kill_process_tree(process,root_identity=worker))
        self.assertEqual([call.args[0]['pid'] for call in terminate.call_args_list],[200])
        process.kill.assert_called_once()

    def test_cli_locks_before_prepare_and_keeps_full_manifest_for_only(self):
        second=copy.deepcopy(self.case);second['id']='other_000';second['group']='other'
        cfg={'output_dir':str(self.root)}
        config_path=self.root/'config.json'
        dump_json(config_path,cfg)
        def preparing(path,config,cases,preview=True):
            self.assertEqual(len(cases),2)
            with self.assertRaises(RuntimeError):
                with QueueLock(self.root):pass
            return self.root
        with patch('sys.argv',['run_experiments.py','plan','--config',str(config_path),'--only','fixture']), \
             patch('run_experiments.load_config',return_value=(cfg,self.root/'config.json')), \
             patch('run_experiments.plan',return_value=[self.case,second]), \
             patch('run_experiments.prepare',side_effect=preparing), \
             patch('run_experiments.execute_command',return_value=0):
            self.assertEqual(main(),0)

    def test_run_returns_failure_for_previously_failed_case_after_report(self):
        dump_json(self.folder/'status.json',{'status':'FAILED','error':'fixture failure'})
        args=SimpleNamespace(command='run',retry_failed=False,limit=0,visible=False)
        with patch('builtins.print'):
            rc=execute_command(args,{'max_case_minutes':1},self.root/'config.json',
                               self.root,[self.case],[self.case])
        self.assertEqual(rc,1)
        self.assertTrue((self.root/'report.html').exists())

    def test_report_rejects_entire_invalid_case_without_csv_rows_or_plots(self):
        values,metadata=self.write_fixture()
        quality=json.loads(values['quality_json'][0])
        del quality['power_closure_p']
        values['quality_json']=np.array([json.dumps(quality)])
        metadata['quality']=quality
        self.rewrite_fixture(values,metadata)
        make_report(self.root,[self.case],{})
        with (self.root/'coefficients_long.csv').open(encoding='utf-8-sig',newline='') as stream:
            self.assertEqual(list(csv.DictReader(stream)),[])
        page=(self.root/'report.html').read_text(encoding='utf8')
        self.assertIn('RESULT_READ_ERROR',page)
        self.assertIn('0/1',page)
        self.assertEqual(list((self.root/'figures').glob('*.png')),[])

    def test_report_does_not_plot_failed_partial_nearfields(self):
        self.case['capture_field']=True
        self.write_fixture()
        failed=copy.deepcopy(self.case);failed['id']='fixture_001';failed['value']=200
        self.write_fixture(failed)
        dump_json(self.root/failed['id']/'status.json',{'status':'FAILED'})
        make_report(self.root,[self.case,failed],{})
        page=(self.root/'report.html').read_text(encoding='utf8')
        self.assertIn('1/2',page)
        self.assertTrue((self.root/'figures'/f'field_{self.case["id"]}_s.png').exists())
        self.assertFalse((self.root/'figures'/f'field_{failed["id"]}_s.png').exists())
        with (self.root/'coefficients_long.csv').open(encoding='utf-8-sig',newline='') as stream:
            rows=list(csv.DictReader(stream))
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[0]['rsp_source'],'analytic_by_2d_symmetry')

    def test_report_preserves_but_never_references_stale_nearfield_images(self):
        self.case['capture_field']=True
        self.write_fixture()
        figdir=self.root/'figures'
        figdir.mkdir()
        stale=figdir/f'field_{self.case["group"]}_old.png'
        old_contents=b'PREVIOUS FINGERPRINT IMAGE FIXTURE; MUST NOT BE READ OR DELETED'
        stale.write_bytes(old_contents)
        make_report(self.root,[self.case],{})
        page=(self.root/'report.html').read_text(encoding='utf8')
        self.assertNotIn(stale.name,page)
        self.assertIn(f'field_{self.case["id"]}_s.png',page)
        self.assertIn(f'field_{self.case["id"]}_p.png',page)
        self.assertEqual(stale.read_bytes(),old_contents)

    def test_phase_near_relative_zero_is_not_interpreted(self):
        values,_=self.write_fixture()
        values['r_E'][0,0,0]=1e-5
        self.assertTrue(np.isnan(_phase_pair(values,1)))

    def test_strict_power_warnings_include_reference_and_relative_discrepancy(self):
        quality={'air_reference_reflected_s':8.5e-5,'air_reference_reflected_p':0.,
                 'total_reflected_s':.01,'total_reflected_p':0.,
                 'flux_decomposition_error_s':.001,'flux_decomposition_error_p':.0001}
        warnings=_strict_power_warnings('measured_case',quality)
        self.assertTrue(any('Air-reference leakage s' in row[1] for row in warnings))
        relative=[row for row in warnings if 'relative discrepancy' in row[1]]
        self.assertEqual(len(relative),2)
        self.assertAlmostEqual(relative[0][2],.1)
        self.assertAlmostEqual(relative[1][2],.1) # raw R=0 uses the fixed 1e-3 floor

    def test_missing_reference_measurement_is_explicit_without_hiding_results(self):
        self.write_fixture() # Deliberately lacks the new independent reference measurement.
        make_report(self.root,[self.case],{})
        page=(self.root/'report.html').read_text(encoding='utf8')
        self.assertIn('Physics check unavailable: missing measured air_reference_reflected_s',page)
        self.assertIn('UNAVAILABLE',page)
        self.assertIn('1/1',page)
        with (self.root/'coefficients_long.csv').open(encoding='utf-8-sig',newline='') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))),3)


if __name__=='__main__':unittest.main()
