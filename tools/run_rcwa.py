"""Isolated RCWA acquisition/calibration. Default `plan` never starts a solver."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ah53runner.planning import load_config, plan
from ah53runner import rcwa


def stamp():
    return datetime.now().astimezone().isoformat()


def output_path(config, path):
    value = Path(config['output_dir']).expanduser()
    return (value if value.is_absolute() else path.parent/value).resolve()


def windows_process_snapshot():
    """Read process identity/parentage; never stop or modify another process."""
    if os.name!='nt':
        return []
    script = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
              "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,"
              "@{Name='Created';Expression={$_.CreationDate.ToUniversalTime().ToString('o')}} "
              "| ConvertTo-Json -Compress")
    result = subprocess.run(['powershell.exe','-NoLogo','-NoProfile','-NonInteractive','-Command',script],
                            capture_output=True,text=True,encoding='utf8',errors='strict',timeout=30,check=True)
    value=json.loads(result.stdout)
    return value if isinstance(value,list) else [value]


def owned_cad_snapshot(worker_pid):
    from run_experiments import process_identity
    rows=windows_process_snapshot()
    by_pid={row['ProcessId']:row for row in rows}
    root=by_pid.get(worker_pid)
    root_created=created_ticks(root.get('Created')) if root else None
    rcwa.require(root_created is not None,'Cannot establish this worker\'s creation time for process ownership.')
    root_identity=process_identity(worker_pid)
    rcwa.require(root_identity is not None and root_identity.get('creation_time_100ns') is not None,
                 'Cannot establish the native worker creation identity.')
    # CIM represents microseconds; the kernel identity retains the final 100ns digit.
    root_native=root_identity['creation_time_100ns']
    rcwa.require(0<=root_native-root_created<=9,'Worker identity changed during the process snapshot.')
    identities={worker_pid:dict(root_identity,ownership_root_pid=worker_pid,
                                ownership_root_creation_time_100ns=root_native)}
    descendants={worker_pid:root_native}
    changed=True
    while changed:
        added={}
        for row in rows:
            if row['ProcessId'] in descendants or row['ParentProcessId'] not in descendants:
                continue
            child_created=created_ticks(row.get('Created'))
            # Windows retains PPID after a parent exits and may recycle that PID.
            # Every edge must connect identities born in chronological order.
            if child_created is None or child_created<descendants[row['ParentProcessId']]//10*10:
                continue
            identity=process_identity(row['ProcessId'])
            native_created=identity.get('creation_time_100ns') if identity else None
            if native_created is None or not (0<=native_created-child_created<=9):
                continue
            if native_created>=descendants[row['ParentProcessId']]:
                added[row['ProcessId']]=native_created
                identities[row['ProcessId']]=dict(identity,parent_pid=row['ParentProcessId'],
                    parent_creation_time_100ns=descendants[row['ParentProcessId']],
                    ownership_root_pid=worker_pid,ownership_root_creation_time_100ns=root_native)
        changed=bool(added)
        descendants.update(added)
    records=[]
    for row in rows:
        if row['ProcessId'] in descendants and row['Name'].lower() in ('fdtd-solutions.exe','rcwa-engine.exe','fdtd-engine.exe'):
            lineage=[]
            current=row['ProcessId']
            while current in identities:
                lineage.append(identities[current])
                if current==worker_pid:
                    break
                current=identities[current]['parent_pid']
            records.append(dict(row,identity=identities[row['ProcessId']],lineage=lineage))
    return records


def created_ticks(value):
    """Parse UTC creation time without discarding the seventh, 100 ns digit."""
    if not isinstance(value,str):
        return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            return None
        fraction=re.search(r'\.(\d+)(?:Z|[+-]\d{2}:\d{2})$',value)
        remainder=int(fraction.group(1)[6]) if fraction and len(fraction.group(1))>=7 else 0
        delta=parsed.astimezone(timezone.utc)-datetime(1601,1,1,tzinfo=timezone.utc)
        return (delta.days*86400+delta.seconds)*10_000_000+delta.microseconds*10+remainder
    except (TypeError,ValueError,OverflowError):
        return None


def active_owned_cads(records):
    from run_experiments import recorded_process_alive
    snapshot=None
    active=[]
    for record in records:
        identity=record.get('identity')
        if isinstance(identity,dict) and identity.get('creation_time_100ns') is not None:
            alive=recorded_process_alive(identity)
        else:
            # Read-only compatibility for earlier timestamped CIM records.
            if snapshot is None:
                snapshot=windows_process_snapshot()
            wanted=created_ticks(record.get('Created'))
            alive=wanted is not None and any(row['ProcessId']==record['ProcessId']
                       and created_ticks(row.get('Created'))==wanted for row in snapshot)
        if alive:
            active.append(record)
    return active


def worker_record(state):
    return {'pid':state.get('worker_pid'),
            'creation_time_100ns':state.get('worker_creation_time_100ns')}


def refuse_active_workers(output):
    """Kernel queue lock alone cannot cover children surviving a parent crash."""
    from run_experiments import recorded_process_alive
    for path in Path(output).glob('*/*/native_status.json'):
        state=json.loads(path.read_text(encoding='utf8'))
        if state.get('status')!='RUNNING' and not state.get('cleanup_failed'):
            continue
        rcwa.require(not recorded_process_alive(worker_record(state)),f'An RCWA worker remains active: {path}')
        tracked=state.get('owned_cad_processes',[])
        if os.name=='nt' and tracked:
            rcwa.require(not active_owned_cads(tracked),
                         f'Owned CAD/solver from an interrupted RCWA worker remains active: {path}')
        else:
            raise rcwa.RCWAError(f'Interrupted RCWA state has no provable CAD cleanup; inspect {path} before modifying that acquisition.')


def refuse_running_fdtd():
    from run_experiments import recorded_process_alive
    for path in ROOT.glob('output_ah53_*/*/status.json'):
        state=json.loads(path.read_text(encoding='utf8'))
        if state.get('status')!='RUNNING' and not state.get('cleanup_failed'):
            continue
        identity=worker_record(state)
        records=[identity]
        root_created=identity['creation_time_100ns']
        records.extend(record for record in state.get('observed_processes',[])
                       if root_created is not None and record.get('creation_time_100ns') is not None
                       and record['creation_time_100ns']>=root_created)
        rcwa.require(not any(recorded_process_alive(record) for record in records),
                     f'FDTD worker/solver is active; RCWA must remain sequential: {path}')


def planned_inputs(config):
    levels = config.get('rcwa_levels')
    rcwa.require(isinstance(levels, list) and bool(levels), 'rcwa_levels must be a nonempty list.')
    rcwa.require(len({level.get('name') for level in levels}) == len(levels), 'Duplicate RCWA level names.')
    for case in plan(config):
        geometry = rcwa.geometry_spec(case['model'])
        for level in levels:
            rcwa.validate_settings(level, case['model'])
            name = level.get('name', '')
            rcwa.require(name and name.replace('_', '').isalnum(), 'RCWA level names must be simple alphanumeric names.')
            yield {'schema_version': '1.0', 'case': case, 'settings': level,
                   'geometry': geometry, 'backend_fingerprint': rcwa.backend_fingerprint(),
                   'api_path': config['lumerical_api_python']}


def prepare(folder, frozen):
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    input_path = folder/'input.json'
    if input_path.exists():
        old = json.loads(input_path.read_text(encoding='utf8'))
        if rcwa.canonical_hash(old) != rcwa.canonical_hash(frozen):
            # Keep each whole previous acquisition together, including its frozen input.
            destination = folder/'attempts'/('input_changed_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
            destination.mkdir(parents=True)
            destination.resolve().relative_to(folder)
            for entry in list(folder.iterdir()):
                if entry.name == 'attempts':
                    continue
                entry.resolve().relative_to(folder)
                shutil.move(str(entry), str(destination/entry.name))
    rcwa.write_json(input_path, frozen)
    (folder/'build_run_export.lsf').write_text(rcwa.build_lsf(frozen['geometry'], frozen['settings']), encoding='utf8')


def origin_plan_folders(config_path, certificate=None):
    """Check current planned weak controls against preserved real frozen inputs.

    This read-only check does not prepare, migrate or acquire any origin data.
    The mathematical gate will independently re-read their full native data.
    """
    config, path = load_config(config_path)
    output = output_path(config, path)
    expected = list(planned_inputs(config))
    rcwa.require(len(expected) == 4, 'Origin configuration must declare exactly four Born acquisitions.')
    folders, identities = [], []
    for frozen in expected:
        folder = output/frozen['case']['id']/frozen['settings']['name']
        actual = json.loads((folder/'input.json').read_text(encoding='utf8'))
        rcwa.require(actual == frozen, 'Actual Born input differs from --origin-config; real current-fingerprint reacquisition is required: '+str(folder))
        folders.append(folder)
        identities.append((str(folder.resolve()), rcwa.canonical_hash(frozen)))
    if certificate is not None:
        evidence = certificate.get('absolute_origin', {}).get('evidence', [])
        recorded = [(str(Path(item['folder']).resolve()), item.get('input_sha256')) for item in evidence]
        rcwa.require(len(recorded) == len(identities) and set(recorded) == set(identities),
                     'Export --origin-config differs from the certificate\'s four actual Born acquisitions.')
    return folders


def read_packed_native_index(session, lumapi):
    """Transfer real RCWA index cells without the API's incompatible point reshape.

The v252 native index z coordinate lists layer interfaces, while its scalar
attribute has one sample per interval. RectilinearDatasetTranslator assumes
point samples at every interface. Preserve the untouched packed attributes and
metadata instead; never infer an attribute-axis ordering or fill missing values.
This hook exists only during this one synchronous transfer in an isolated worker.
"""
    translator=lumapi.GetTranslator
    original_descriptor=vars(translator).get('applyLumDatasetConventions')
    rcwa.require(original_descriptor is not None,'Cannot locate the installed native dataset-convention translator.')
    original=translator.applyLumDatasetConventions
    skipped=[]
    def preserve_rectilinear(dataset):
        metadata=dataset.get('Lumerical_dataset') if isinstance(dataset,dict) else None
        if isinstance(metadata,dict) and metadata.get('geometry')=='rectilinear':
            skipped.append(True)
            return
        return original(dataset)
    translator.applyLumDatasetConventions=staticmethod(preserve_rectilinear)
    try:
        raw=session.getresult('RCWA','index')
    finally:
        translator.applyLumDatasetConventions=original_descriptor
    metadata=raw.get('Lumerical_dataset') if isinstance(raw,dict) else None
    rcwa.require(len(skipped)==1 and isinstance(metadata,dict)
                 and metadata.get('geometry')=='rectilinear','Expected exactly one native rectilinear index dataset.')
    coordinates={}
    for name in ('x','y','z','lambda','f'):
        rcwa.require(name in raw,f'Native index lacks coordinate {name}.')
        value=np.asarray(raw[name])
        rcwa.require(not value.dtype.hasobject and value.size>0 and np.isfinite(value).all()
                     and sum(length!=1 for length in value.shape)<=1,f'Invalid native index coordinate {name}.')
        coordinates[name]=value.size
    rcwa.require(coordinates['z']>=2 and coordinates['lambda']==coordinates['f'],
                 'Native index needs layer interfaces and matching wavelength/frequency axes.')
    attributes=metadata.get('attributes',[])
    rcwa.require(bool(attributes) and all(name in ('index_x','index_y','index_z') for name in attributes),
                 'Unexpected native index attributes; refusing an unvalidated interpretation.')
    expected_cells=coordinates['x']*coordinates['y']*(coordinates['z']-1)*coordinates['lambda']
    shapes={}
    for name in attributes:
        rcwa.require(name in raw,f'Missing native index attribute {name}.')
        value=np.asarray(raw[name])
        rcwa.require(not value.dtype.hasobject and np.isfinite(value).all()
                     and value.size==expected_cells,
                     f'Native {name} packed element count does not match the observed layer-interval count.')
        shapes[name]=list(value.shape)
    description={'mode':'native_packed_cell_attributes_without_rectilinear_reshape',
                 'reason':'v252 RCWA index samples layer intervals; bundled rectilinear translator assumes interface points',
                 'coordinate_lengths':coordinates,'z_interface_count':coordinates['z'],
                 'z_layer_interval_count':coordinates['z']-1,
                 'native_attribute_shapes':shapes,'native_elements_per_attribute':expected_cells,
                 'attribute_reshape_applied':False,'interpolation_or_padding_applied':False,
                 'original_metadata_and_arrays_preserved':True}
    return raw,description


def worker(folder):
    """One new hidden CAD session; only the parent finalizes complete checksums."""
    folder = Path(folder).resolve()
    frozen = json.loads((folder/'input.json').read_text(encoding='utf8'))
    state = {'status': 'RUNNING', 'started_at': stamp(), 'worker_pid': os.getpid(),
             'backend_fingerprint': rcwa.backend_fingerprint(),
             'input_sha256': rcwa.canonical_hash(frozen), 'phase_status': 'UNCALIBRATED'}
    from run_experiments import process_identity
    identity=process_identity(os.getpid())
    rcwa.require(identity is not None,'Cannot establish RCWA worker identity.')
    state['worker_creation_time_100ns']=identity.get('creation_time_100ns')
    rcwa.write_json(folder/'native_status.json', state)
    try:
        rcwa.require(frozen['backend_fingerprint'] == rcwa.backend_fingerprint(), 'Backend changed after planning.')
        refuse_running_fdtd()
        rcwa.validate_settings(frozen['settings'], frozen['case']['model'])
        rcwa.require(frozen['geometry'] == rcwa.geometry_spec(frozen['case']['model']), 'Frozen geometry is not the shared geometry.')
        api_path = Path(frozen['api_path']).resolve()
        rcwa.require((api_path/'lumapi.py').is_file(), 'Configured local lumapi.py does not exist.')
        sys.path.insert(0, str(api_path))
        lumapi = importlib.import_module('lumapi')
        print('START NATIVE RCWA', folder, flush=True)
        print('INPUT:', state['input_sha256'], flush=True)
        with lumapi.FDTD(hide=True) as session:
            state['lumerical_version']=str(session.version())
            rcwa.require(state['lumerical_version'].startswith('8.34.'),'This backend is scoped to installed 2025 R2, not this solver version.')
            state['owned_cad_processes']=owned_cad_snapshot(os.getpid())
            rcwa.require(os.name!='nt' or bool(state['owned_cad_processes']), 'Cannot identify this worker\'s own CAD process; refusing an untracked run.')
            rcwa.write_json(folder/'native_status.json',state)
            session.cd(str(folder))
            script = (folder/'build_run_export.lsf').read_text(encoding='utf8')
            rcwa.require(script == rcwa.build_lsf(frozen['geometry'], frozen['settings']), 'Execution script differs from the frozen generated script.')
            session.eval(script)
            for name in rcwa.DATASETS:
                print('TRANSFER_NATIVE_DATASET:',name,flush=True)
                if name=='index':
                    dataset,state['native_index_transfer']=read_packed_native_index(session,lumapi)
                else:
                    dataset=session.getresult('RCWA',name)
                rcwa.save_native_dataset(dataset, folder, name)
                print('SAVED_NATIVE_DATASET:',name,flush=True)
            state['actual_solver'] = {}
            for name in ('num_k','simulation_run_time','total_threads','peak_memory'):
                state['actual_solver'][name] = np_value(session.getresult('RCWA',name))
            for prop in ('interface actual positions','actual mesh cells x'):
                state['actual_solver'][prop] = np_value(session.getnamed('RCWA',prop))
            state['resource_policy'] = 'Independent hidden session; existing RCWA resource settings unchanged.'
        deadline=time.monotonic()+15
        while active_owned_cads(state['owned_cad_processes']) and time.monotonic()<deadline:
            time.sleep(.25)
        state['cleanup_failed']=bool(active_owned_cads(state['owned_cad_processes']))
        rcwa.require(not state['cleanup_failed'],'Owned CAD did not close after its API session; stopping to prevent overlapping sessions.')
        state.update(status='RAW_COLLECTED', finished_at=stamp(), session_closed=True)
        print('RAW COLLECTED; PHASE IS UNCALIBRATED', flush=True)
        rcwa.write_json(folder/'native_status.json',state)
        return 0
    except Exception as error:
        traceback.print_exc()
        state.update(status='FAILED', finished_at=stamp(), error=repr(error))
        rcwa.write_json(folder/'native_status.json',state)
        return 1


def np_value(value):
    return value.tolist() if hasattr(value,'tolist') else value


def acquire(folder, timeout_seconds):
    from run_experiments import kill_process_tree, process_identity
    folder = Path(folder).resolve()
    command = [sys.executable, str(Path(__file__).resolve()), '_worker', '--worker-folder', str(folder)]
    prior = [entry for entry in folder.iterdir() if entry.name not in ('input.json','build_run_export.lsf','attempts')]
    if prior:
        destination = folder/'attempts'/('retry_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
        destination.mkdir(parents=True)
        destination.resolve().relative_to(folder)
        for source in (folder/'input.json', folder/'build_run_export.lsf'):
            shutil.copy2(source, destination/source.name)
        for source in prior:
            source.resolve().relative_to(folder)
            shutil.move(str(source),str(destination/source.name))
    code = 1
    with (folder/'worker.log').open('w',encoding='utf8') as log:
        log.write(json.dumps({'started_at':stamp(),'command':command},ensure_ascii=False)+'\n')
        log.flush()
        options = {'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP} if os.name=='nt' else {'start_new_session':True}
        try:
            process = subprocess.Popen(command,cwd=folder,stdout=log,stderr=subprocess.STDOUT,**options)
        except OSError as error:
            traceback.print_exc(file=log)
            rcwa.write_json(folder/'native_status.json',{'status':'FAILED','finished_at':stamp(),
                            'returncode':1,'error':'Worker spawn failed: '+repr(error)})
            return 1
        identity=process_identity(process.pid)
        rcwa.write_json(folder/'native_status.json',{'status':'RUNNING','worker_pid':process.pid,
                        'worker_creation_time_100ns':identity.get('creation_time_100ns') if identity else None,
                        'parent_pid':os.getpid(),'started_at':stamp(),'phase_status':'UNCALIBRATED'})
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            code = 124
            log.write('\nTIMEOUT; stopping only this worker process tree.\n')
        except KeyboardInterrupt:
            code = 130
            log.write('\nINTERRUPTED; stopping only this worker process tree.\n')
        finally:
            if process.poll() is None:
                state=json.loads((folder/'native_status.json').read_text(encoding='utf8'))
                observed=[]
                for record in state.get('owned_cad_processes',[]):
                    observed.extend(record.get('lineage',[]))
                    if isinstance(record.get('identity'),dict):
                        observed.append(record['identity'])
                if not kill_process_tree(process,observed_processes=observed,root_identity=identity):
                    state.update(status='FAILED',cleanup_failed=True,error='Own worker process tree cleanup failed.',finished_at=stamp())
                    rcwa.write_json(folder/'native_status.json',state)
                    raise RuntimeError('Own RCWA worker cleanup failed; queue must stop.')
        log.write(f'\nWORKER_EXIT_CODE={code}\n')
    status_path = folder/'native_status.json'
    state = json.loads(status_path.read_text(encoding='utf8')) if status_path.exists() else {}
    if code == 0 and state.get('status') == 'RAW_COLLECTED':
        state['file_sha256'] = {entry.name:rcwa.sha256(entry) for entry in folder.iterdir()
                                if entry.is_file() and entry.name!='native_status.json'}
        state['status'] = 'RAW_COMPLETE_PHASE_UNCALIBRATED'
        state['returncode'] = code
        rcwa.write_json(status_path,state)
        try:
            rcwa.read_acquisition(folder)  # Missing/corrupt arrays cannot be silently completed.
        except (OSError,ValueError,KeyError,TypeError) as error:
            state.update(status='FAILED',error='Native artifact validation: '+str(error),returncode=1)
            rcwa.write_json(status_path,state)
            return 1
    else:
        state.update(status='FAILED',returncode=code,finished_at=stamp())
        rcwa.write_json(status_path,state)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='plan', choices=('plan','run','calibrate','export','_worker'))
    parser.add_argument('--config', default=str(ROOT/'configs'/'rcwa_validation.json'))
    parser.add_argument('--origin-config', default=str(ROOT/'configs'/'rcwa_origin_validation.json'),
                        help='Independent four-control Born input plan, required for calibration/export; never starts it automatically.')
    parser.add_argument('--only',default='',help='One experiment group.')
    parser.add_argument('--level',default='',help='One declared RCWA mesh level.')
    parser.add_argument('--limit',type=int,default=0)
    parser.add_argument('--retry-failed',action='store_true')
    parser.add_argument('--registration',choices=rcwa.REGISTRATIONS,default='native',
                        help='Declared hypothesis; only cell_origin_half_cell with independent absolute Born evidence can certify x=0.')
    parser.add_argument('--worker-folder',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.command == '_worker':
        rcwa.require(bool(args.worker_folder),'Missing internal worker folder.')
        return worker(args.worker_folder)
    config,path = load_config(args.config)
    output = output_path(config,path)
    frozen_inputs = list(planned_inputs(config))
    selected = [frozen for frozen in frozen_inputs if (not args.only or frozen['case']['group']==args.only)
                and (not args.level or frozen['settings']['name']==args.level)]
    rcwa.require(bool(selected),'No matching RCWA acquisitions.')
    rcwa.require(args.limit>=0,'--limit must be nonnegative.')
    folders = [output/frozen['case']['id']/frozen['settings']['name'] for frozen in selected]
    from run_experiments import QueueLock
    with QueueLock(output):
        refuse_active_workers(output)
        if args.command in ('plan','run'):
            for folder,frozen in zip(folders,selected):
                prepare(folder,frozen)
            if args.command=='plan':
                for folder in folders:
                    print('PLANNED:',folder,flush=True)
                print('NO SOLVER RUNS WERE PERFORMED.',flush=True)
                return 0
            # RCWA acquisition remains downstream of the same real FDTD physics and quick gates.
            sys.path.insert(0,str(ROOT/'tools'))
            from run_validated_overnight import evaluate_gate
            gate,unused = evaluate_gate(ROOT/'configs'/'physics_validation.json',ROOT/'configs'/'quick_check.json')
            rcwa.require(gate['status']=='PASS','FDTD physics/quick gate must PASS before RCWA acquisition: '+'; '.join(gate['errors']))
            refuse_running_fdtd()
            count = 0
            failed = False
            for folder in folders:
                try:
                    rcwa.read_acquisition(folder)
                    print('SKIP COMPLETE RAW:',folder,flush=True)
                    continue
                except (OSError,ValueError,KeyError):
                    pass
                status_path=folder/'native_status.json'
                try:
                    prior=json.loads(status_path.read_text(encoding='utf8')).get('status') if status_path.exists() else None
                except (OSError,ValueError,AttributeError):
                    prior='FAILED'
                if args.retry_failed and prior is None:
                    continue
                if not args.retry_failed and prior=='FAILED':
                    continue
                if args.limit and count>=args.limit:
                    break
                refuse_active_workers(output)
                refuse_running_fdtd()
                count+=1
                code=acquire(folder,int(config.get('max_case_minutes',15))*60)
                failed |= code!=0
                if code==130:
                    return code
            return 1 if failed else 0
        certificate_path=output/'rcwa_phase_calibration.json'
        if args.command=='calibrate':
            try:
                origin_folders=origin_plan_folders(args.origin_config)
                certificate=rcwa.calibrate(folders,args.registration,origin_folders)
            except (OSError,ValueError,KeyError,TypeError) as error:
                certificate={'schema_version':'2.0','status':'FAIL','backend_fingerprint':rcwa.backend_fingerprint(),
                             'registration':args.registration,'error':str(error),'evaluated_at':stamp()}
            attempt_path=output/'calibration_attempts'/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'_'+args.registration+'.json')
            rcwa.write_json(attempt_path,certificate)
            rcwa.write_json(certificate_path,certificate)
            print('RCWA PHASE CALIBRATION:',certificate['status'],certificate_path,flush=True)
            print('PRESERVED CALIBRATION ATTEMPT:',attempt_path,flush=True)
            if certificate.get('error'):
                print(certificate['error'],flush=True)
            return 0 if certificate['status']=='PASS' else 2
        certificate=json.loads(certificate_path.read_text(encoding='utf8'))
        origin_plan_folders(args.origin_config,certificate)
        for folder in folders:
            print('EXPORTED:',rcwa.export_calibrated(folder,certificate),flush=True)
        return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError) as error:
        print('RCWA REFUSED:',error,file=sys.stderr,flush=True)
        raise SystemExit(2)
