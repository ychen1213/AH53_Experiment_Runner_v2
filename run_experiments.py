"""AH53 batch runner. JSON config, one isolated subprocess per solver case.

COMMANDS: check, plan, run, report. Safe `plan` does not launch Lumerical.
"""
from __future__ import annotations
import argparse
import csv
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from ah53runner.geometry import build_geometry,shapes_to_json
from ah53runner.planning import load_config,plan
from ah53runner.artifacts import ArtifactError, load_artifacts, solver_fingerprint, strict_json_loads

ROOT=Path(__file__).resolve().parent

def dump_json(path,value):
    path=Path(path)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf8')
    tmp.replace(path)

def status_of(path):
    try:
        value=strict_json_loads(path.read_text(encoding='utf8'))
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):
        return None

def result_valid(folder,case):
    try:
        load_artifacts(folder,case)
        return True
    except ArtifactError:
        return False


def process_alive(pid):
    if not isinstance(pid,int) or pid<=0:return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return ctypes.get_last_error()==5 # access denied: conservatively alive
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except PermissionError:return True
    return True


def process_identity(pid):
    """A Windows creation time prevents a recycled PID matching an old worker."""
    if os.name!='nt':return {'pid':pid,'creation_time_100ns':None}
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000,False,pid)
    if not handle:return None
    try:
        creation,finish,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle,ctypes.byref(creation),ctypes.byref(finish),
                                      ctypes.byref(kernel_time),ctypes.byref(user_time)):
            return None
        return {'pid':pid,'creation_time_100ns':(creation.dwHighDateTime<<32)|creation.dwLowDateTime}
    finally:kernel.CloseHandle(handle)


def recorded_process_alive(record):
    if not isinstance(record,dict):return False
    pid=record.get('pid')
    expected=record.get('creation_time_100ns')
    if not isinstance(pid,int) or pid<=0 or not isinstance(expected,int) or expected<=0:return False
    actual=process_identity(pid)
    # An inaccessible or recycled PID is not evidence of an owned process.
    return (actual is not None and actual.get('creation_time_100ns')==expected
            and process_alive(pid))


def causal_process_tree(root_identity,records):
    """Require a creation-time causal edge at every step, never just ParentPID."""
    if not root_identity:return {}
    root_pid=root_identity.get('pid');root_time=root_identity.get('creation_time_100ns')
    if not isinstance(root_time,int) or root_time<=0:return {}
    root=records.get(root_pid)
    if not root or root.get('creation_time_100ns')!=root_time:return {}
    owned={root_pid:dict(root)}
    while True:
        found={}
        for pid,record in records.items():
            if pid in owned:continue
            parent=owned.get(record.get('parent_pid'))
            born=record.get('creation_time_100ns')
            if parent is None or not isinstance(born,int) or born<parent['creation_time_100ns']:continue
            child=dict(record)
            child.update(parent_creation_time_100ns=parent['creation_time_100ns'],
                         ownership_root_pid=root_pid,ownership_root_creation_time_100ns=root_time)
            found[pid]=child
        if not found:break
        owned.update(found)
    return owned


def state_process_records(state):
    """Ignore impossible legacy observations made before the worker existed."""
    root_pid=state.get('worker_pid');root_time=state.get('worker_creation_time_100ns')
    records=[r for r in state.get('observed_processes',[]) if isinstance(r,dict)]
    if not isinstance(root_time,int):
        root_time=next((r.get('creation_time_100ns') for r in records if r.get('pid')==root_pid),None)
    if not isinstance(root_time,int) or root_time<=0:return []
    records.append({'pid':root_pid,'creation_time_100ns':root_time})
    return [r for r in records if isinstance(r.get('creation_time_100ns'),int)
            and r['creation_time_100ns']>=root_time
            and (r.get('parent_creation_time_100ns') is None or
                 r['creation_time_100ns']>=r['parent_creation_time_100ns'])]


class QueueLock:
    """Kernel-owned lock; a crash releases it without unsafe stale-file deletion."""
    def __init__(self,output):
        self.output=Path(output)
        self.stream=None

    def __enter__(self):
        self.output.mkdir(parents=True,exist_ok=True)
        descriptor=os.open(str(self.output/'.queue.lock'),os.O_CREAT|os.O_RDWR,0o600)
        self.stream=os.fdopen(descriptor,'r+b')
        try:
            if self.stream.seek(0,os.SEEK_END)==0:
                self.stream.write(b'\0');self.stream.flush()
            self.stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close();self.stream=None
            raise RuntimeError(f'Another command holds the queue lock: {self.output}') from exc
        try:
            # After a parent crash an isolated worker may still own a solver.
            # Refuse another launch until that recorded worker has exited.
            for status_path in self.output.glob('*/status.json'):
                state=status_of(status_path) or {}
                if state.get('status')!='RUNNING' and not state.get('cleanup_failed'):continue
                records=state_process_records(state)
                active=[record for record in records if recorded_process_alive(record)]
                if active:
                    raise RuntimeError(f'Worker/solver PID {active[0]["pid"]} is still active: {status_path}')
            self.stream.seek(1)
            self.stream.write(json.dumps({'pid':os.getpid()}).encode('utf8'))
            self.stream.truncate();self.stream.flush()
            return self
        except BaseException:
            self.__exit__(None,None,None)
            raise

    def __exit__(self,*unused):
        if self.stream is not None:
            self.stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(),fcntl.LOCK_UN)
            self.stream.close();self.stream=None


def archive_previous_attempt(folder):
    """Retain previous logs, model, partial arrays, and frozen input together."""
    folder=Path(folder)
    inputs={'case.json','geometry.json','geometry_preview.png'}
    generated=[p for p in folder.iterdir() if p.name not in inputs|{'attempts'}]
    if not generated:return None
    prior=status_of(folder/'status.json') or {}
    attempt=prior.get('attempt_id') or ('legacy_'+uuid.uuid4().hex[:12])
    destination=folder/'attempts'/attempt
    if destination.exists():destination=destination.with_name(attempt+'_'+uuid.uuid4().hex[:8])
    destination.mkdir(parents=True)
    for name in inputs:
        if (folder/name).is_file():shutil.copy2(folder/name,destination/name)
    for entry in generated:shutil.move(str(entry),str(destination/entry.name))
    return destination


def should_run_case(folder,case,retry_failed=False):
    if result_valid(folder,case):return False
    status=(status_of(Path(folder)/'status.json') or {}).get('status','PENDING')
    if retry_failed:return status in ('FAILED','RUNNING','DONE','INCOMPLETE','INTERRUPTED')
    return status!='FAILED'

def write_preview(folder,shapes,case):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle,Polygon
    fig,ax=plt.subplots(figsize=(10,2.8))
    for s in shapes:
        if s.vertices_nm:
            patch=Polygon(s.vertices_nm,alpha=.25,edgecolor='black',linewidth=.8)
        else:
            patch=Rectangle((s.x1_nm,s.y1_nm),s.x2_nm-s.x1_nm,s.y2_nm-s.y1_nm,
                            alpha=.25,edgecolor='black',linewidth=.8)
        ax.add_patch(patch)
    ax.set_xlim(-case['model']['period_nm']/2,case['model']['period_nm']/2)
    ax.set_ylim(-700,max([s.y2_nm for s in shapes])+200)
    ax.set_xlabel('X (nm)')
    ax.set_ylabel('Y (nm)')
    ax.set_title(f"{case['group']} | {case['parameter']}={case['value']} (geometry only)")
    ax.grid(alpha=.25)
    fig.tight_layout()
    fig.savefig(folder/'geometry_preview.png',dpi=135)
    plt.close(fig)


def prepare(path,cfg,cases,preview=True):
    output=Path(cfg['output_dir']).expanduser()
    if not output.is_absolute(): output=path.parent/output
    output=output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    for c in cases:
        c['solver_fingerprint']=solver_fingerprint()
        folder=output/c['id']
        folder.mkdir(exist_ok=True)
        casefile=folder/'case.json'
        refresh_preview=False
        if casefile.exists():
            old=status_of(casefile)
            if old is not None and old.get('digest')!=c['digest']:
                raise ValueError(f'Case ID collision at {folder}')
            if (old is None or old.get('model')!=c['model'] or
                old.get('solver_fingerprint')!=c['solver_fingerprint'] or
                old.get('capture_field')!=c.get('capture_field')):
                refresh_preview=True
                archived=archive_previous_attempt(folder)
                if archived:
                    dump_json(folder/'status.json',{'status':'INCOMPLETE','digest':c['digest'],
                              'solver_fingerprint':c['solver_fingerprint'],
                              'error':'Execution fingerprint or requested artifacts changed; previous attempt retained',
                              'previous_attempt':str(archived.relative_to(folder))})
        dump_json(casefile,c)
        if preview:
            shapes,top,meta=build_geometry(c['model'])
            dump_json(folder/'geometry.json',{'shapes':shapes_to_json(shapes),'info':meta})
            if refresh_preview or c.get('capture_field') or not (folder/'geometry_preview.png').exists():
                write_preview(folder,shapes,c)
    dump_json(output/'manifest.json',{'schema_version':'2.0','config_file':str(path),
                                     'cases':[{'id':c['id'],'digest':c['digest'],
                                               'group':c['group'],'parameter':c['parameter'],
                                               'value':c['value'],
                                               'solver_fingerprint':c['solver_fingerprint']} for c in cases]})
    return output


def task_summary(output,cases):
    out={'DONE':0,'FAILED':0,'RUNNING':0,'PENDING':0}
    for c in cases:
        folder=output/c['id']
        st=status_of(folder/'status.json')
        status=st.get('status') if st else 'PENDING'
        if status=='DONE' and not result_valid(folder,c):
            status='FAILED'
        if status=='RUNNING' and not process_alive(st.get('worker_pid')):status='FAILED'
        if status in ('INCOMPLETE','INTERRUPTED'):status='FAILED'
        if status not in out: status='PENDING'
        out[status]+=1
    return out


def terminate_verified_process(record):
    """Terminate the same kernel process handle whose creation time was checked."""
    if not recorded_process_alive(record):return True
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(0x1000|0x0001|0x00100000,False,record['pid'])
    if not handle:return not recorded_process_alive(record)
    try:
        creation,finish,kt,ut=(wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle,ctypes.byref(creation),ctypes.byref(finish),ctypes.byref(kt),ctypes.byref(ut)):
            return False
        actual=(creation.dwHighDateTime<<32)|creation.dwLowDateTime
        if actual!=record['creation_time_100ns']:return True
        if not kernel.TerminateProcess(handle,130):return not recorded_process_alive(record)
        return kernel.WaitForSingleObject(handle,5000)==0
    finally:kernel.CloseHandle(handle)


def kill_process_tree(proc,observed_processes=None,root_identity=None):
    if os.name=='nt':
        root_identity=root_identity or (process_identity(proc.pid) if proc.poll() is None else None)
        owned=causal_process_tree(root_identity,windows_process_snapshot()) if root_identity else {}
        # Historical observations are eligible only with an explicit causal
        # parent identity and the same ownership root, never a naked PID list.
        if root_identity:
            prior={r['pid']:r for r in (observed_processes or []) if isinstance(r,dict)
                   and r.get('ownership_root_pid')==root_identity['pid']
                   and r.get('ownership_root_creation_time_100ns')==root_identity['creation_time_100ns']
                   and isinstance(r.get('parent_creation_time_100ns'),int)
                   and isinstance(r.get('creation_time_100ns'),int)
                   and r['creation_time_100ns']>=r['parent_creation_time_100ns']>=root_identity['creation_time_100ns']}
            prior[root_identity['pid']]=dict(root_identity)
            # Supply the root node for the pure causal-tree check.
            owned.update(causal_process_tree(root_identity,prior))
        safe=True
        for record in sorted(owned.values(),key=lambda r:r['creation_time_100ns'],reverse=True):
            if record['pid']==proc.pid:continue
            safe=terminate_verified_process(record) and safe
        # Popen owns an original kernel handle even if the numeric PID is reused.
        # Its kill method cannot target another process with the recycled PID.
        if proc.poll() is None:proc.kill()
    else:
        if proc.poll() is not None:return True
        import signal
        try: os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError: pass
        safe=True
    try:proc.wait(timeout=8)
    except subprocess.TimeoutExpired:return False
    return safe and proc.poll() is not None


def windows_process_snapshot():
    """Read parent PIDs and resolvable creation identities; inaccessible = absent."""
    if os.name!='nt':return {}
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    class ProcessEntry(ctypes.Structure):
        _fields_=[('dwSize',wintypes.DWORD),('cntUsage',wintypes.DWORD),
                  ('th32ProcessID',wintypes.DWORD),('th32DefaultHeapID',ctypes.c_size_t),
                  ('th32ModuleID',wintypes.DWORD),('cntThreads',wintypes.DWORD),
                  ('th32ParentProcessID',wintypes.DWORD),('pcPriClassBase',wintypes.LONG),
                  ('dwFlags',wintypes.DWORD),('szExeFile',wintypes.WCHAR*260)]
    kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
    kernel.Process32FirstW.argtypes=[wintypes.HANDLE,ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.argtypes=[wintypes.HANDLE,ctypes.POINTER(ProcessEntry)]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot==ctypes.c_void_p(-1).value:return {}
    parents={}
    try:
        entry=ProcessEntry();entry.dwSize=ctypes.sizeof(entry)
        ok=kernel.Process32FirstW(snapshot,ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)]=int(entry.th32ParentProcessID)
            ok=kernel.Process32NextW(snapshot,ctypes.byref(entry))
    finally:kernel.CloseHandle(snapshot)
    records={}
    for pid,parent in parents.items():
        identity=process_identity(pid)
        if identity is not None:
            records[pid]=dict(identity,parent_pid=parent)
    return records


def sample_process_tree_memory(root_pid,observations=None):
    """Sample only descendants whose birth follows their actual parent's birth."""
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    psapi=ctypes.WinDLL('psapi',use_last_error=True)
    class MemoryCounters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[
            (name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize',
            'QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(MemoryCounters),wintypes.DWORD]
    records=windows_process_snapshot()
    descendants=causal_process_tree(process_identity(root_pid),records)
    total=0;read_count=0
    for pid,identity in descendants.items():
        if not recorded_process_alive(identity):continue
        handle=kernel.OpenProcess(0x0400|0x0010,False,pid)
        if not handle:continue
        try:
            created,ended,kt,ut=(wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle,ctypes.byref(created),ctypes.byref(ended),ctypes.byref(kt),ctypes.byref(ut)):
                continue
            if ((created.dwHighDateTime<<32)|created.dwLowDateTime)!=identity['creation_time_100ns']:
                continue
            counters=MemoryCounters();counters.cb=ctypes.sizeof(counters)
            if psapi.GetProcessMemoryInfo(handle,ctypes.byref(counters),ctypes.sizeof(counters)):
                total+=int(counters.WorkingSetSize);read_count+=1
                if observations is not None:
                    observations[pid]=identity
        finally:kernel.CloseHandle(handle)
    return (total,read_count) if read_count else None


def wait_with_memory_samples(proc,timeout_s,telemetry=None,on_sample=None):
    deadline=time.monotonic()+timeout_s
    peak=0;peak_count=0;samples=0
    telemetry={} if telemetry is None else telemetry
    telemetry.update(sampled_peak_tree_working_set_bytes=None,memory_sample_count=0,
                     memory_sample_interval_s=2.,max_sampled_tree_process_count=None)
    observations={}
    while True:
        try:sample=sample_process_tree_memory(proc.pid,observations)
        except Exception:sample=None # optional telemetry cannot change solver success
        if sample is not None:
            peak=max(peak,sample[0]);peak_count=max(peak_count,sample[1]);samples+=1
            telemetry.update(sampled_peak_tree_working_set_bytes=peak,memory_sample_count=samples,
                             max_sampled_tree_process_count=peak_count)
        telemetry['observed_processes']=list(observations.values())
        if on_sample is not None:on_sample()
        remaining=deadline-time.monotonic()
        if remaining<=0:raise subprocess.TimeoutExpired(proc.args,timeout_s)
        try:
            rc=proc.wait(timeout=min(2.,remaining))
            return rc
        except subprocess.TimeoutExpired:
            if time.monotonic()>=deadline:raise


def observed_descendants_exited(state):
    records=[record for record in state_process_records(state)
             if record.get('pid')!=state.get('worker_pid')]
    deadline=time.monotonic()+2.
    while any(recorded_process_alive(record) for record in records):
        if time.monotonic()>=deadline:return False
        time.sleep(.1)
    return True


def launch_case(output,case,api_dir,timeout_s,visible):
    folder=output/case['id']
    archive_previous_attempt(folder)
    log_path=folder/'worker.log'
    status=folder/'status.json'
    now=lambda:datetime.datetime.now().astimezone().isoformat(timespec='seconds')
    state={'status':'RUNNING','started_at':now(),'digest':case['digest'],
           'solver_fingerprint':case['solver_fingerprint'],'attempt_id':uuid.uuid4().hex,
           'worker_pid':None,'parent_pid':os.getpid(),'worker_log':'worker.log'}
    dump_json(status,state)
    cmd=[sys.executable,str(ROOT/'run_case_worker.py'),'--case',str(folder/'case.json'),
         '--api-dir',str(api_dir or '')]
    if visible: cmd.append('--visible')
    begin=time.monotonic()
    proc=None
    rc=None
    timedout=False
    interrupted=False
    error=None
    cleanup_failed=False
    with log_path.open('w',encoding='utf8') as log:
        log.write(f'\n===== {now()} START {case["id"]} =====\n')
        log.flush()
        kw={}
        if os.name=='nt':
            kw['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kw['start_new_session']=True
        try:
            proc=subprocess.Popen(cmd,cwd=str(folder),stdout=log,stderr=subprocess.STDOUT,**kw)
            state['worker_pid']=proc.pid
            identity=process_identity(proc.pid)
            state['worker_creation_time_100ns']=identity.get('creation_time_100ns') if identity else None
            dump_json(status,state)
            rc=wait_with_memory_samples(proc,timeout_s,state,on_sample=lambda:dump_json(status,state))
        except subprocess.TimeoutExpired:
            timedout=True
            rc=124
            error='case timeout'
        except KeyboardInterrupt:
            interrupted=True
            rc=130
            error='interrupted by user; stopped own worker process tree'
        except Exception as exc:
            error=repr(exc)
            traceback.print_exc(file=log)
        finally:
            if proc is not None and proc.poll() is None:
                try:cleanup_failed=not kill_process_tree(proc,state.get('observed_processes'),
                      {'pid':state['worker_pid'],'creation_time_100ns':state.get('worker_creation_time_100ns')})
                except Exception as exc:
                    cleanup_failed=True
                    error=(error or '')+'; cleanup: '+repr(exc)
            if not observed_descendants_exited(state):
                cleanup_failed=True
                error=(error or '')+'; observed solver descendants remain active; queue stopped'
        elapsed=time.monotonic()-begin
        log.write(f'===== END rc={rc} elapsed_s={elapsed:.1f} timeout={timedout} =====\n')
        success=rc==0 and not interrupted and not cleanup_failed and error is None
        if success:
            try:load_artifacts(folder,case,require_done=False)
            except ArtifactError as exc:
                success=False
                error='artifact validation: '+str(exc)
        if error:log.write('ERROR: '+error+'\n')
    state.update(status='DONE' if success else 'FAILED',finished_at=now(),elapsed_s=elapsed,
                 returncode=rc,timeout=timedout,interrupted=interrupted,error=error,
                 cleanup_failed=cleanup_failed)
    dump_json(status,state)
    if cleanup_failed:
        raise RuntimeError(f'Worker cleanup failed; stopping queue to avoid concurrent solvers: {folder}')
    if interrupted:raise KeyboardInterrupt
    return success,elapsed


def main():
    p=argparse.ArgumentParser(description='Lumerical 2023 R2.1 AH53 batch experiment runner')
    p.add_argument('command',choices=['check','plan','run','report'])
    p.add_argument('--config',default=str(ROOT/'configs'/'quick_check.json'))
    p.add_argument('--only',default='',help='Run/plan/report only one experiment group')
    p.add_argument('--limit',type=int,default=0,help='Max number of cases to execute this time')
    p.add_argument('--retry-failed',action='store_true',help='Only retry failed, interrupted, or incomplete previous cases')
    p.add_argument('--visible',action='store_true',help='Show Lumerical GUI (diagnostic use)')
    args=p.parse_args()
    cfg,path=load_config(args.config)
    all_cases=plan(cfg)
    cases=all_cases
    if args.only:
        cases=[c for c in cases if c['group']==args.only]
        if not cases: raise SystemExit(f'No cases for {args.only}')
    if args.limit<0:raise ValueError('--limit must be >=0')
    output=Path(cfg['output_dir']).expanduser()
    if not output.is_absolute():output=path.parent/output
    output=output.resolve()
    with QueueLock(output):
        prepare(path,cfg,all_cases,preview=(args.command in ('check','plan','run')))
        return execute_command(args,cfg,path,output,cases,all_cases)


def execute_command(args,cfg,path,output,cases,all_cases):
    if args.command in ('check','plan'):
        print(f'CONFIG: {path}\nOUT: {output}\nCASES: {len(cases)}')
        for c in cases:
            print(' ',c['id'],'=>',c['parameter'],'=',c['value'], 'nearfield=',bool(c.get('capture_field')))
        print('STATE:',task_summary(output,cases))
        print('NO SOLVER RUNS WERE PERFORMED.')
        return
    if args.command=='report':
        from ah53runner.report import make_report
        make_report(output,cases,cfg)
        print('Report:',output/'report.html')
        return
    # run: all cases sequentially, each in a separate process and Lumerical session.
    timeout_s=int(cfg.get('max_case_minutes',90))*60
    if timeout_s<60: raise ValueError('max_case_minutes must be >=1')
    count=0
    interrupted=False
    try:
        for c in cases:
            folder=output/c['id']
            if not should_run_case(folder,c,args.retry_failed):
                print('SKIP',c['id'],(status_of(folder/'status.json') or {}).get('status','PENDING'),flush=True)
                continue
            if args.limit and count>=args.limit: break
            count+=1
            print('START',c['id'],flush=True)
            success,elapsed=launch_case(output,c,cfg.get('lumerical_api_python',''),timeout_s,args.visible)
            print('OK' if success else 'FAILED',c['id'],f'{elapsed:.1f}s',flush=True)
    except KeyboardInterrupt:
        interrupted=True
        print('INTERRUPTED: worker stopped and failure recorded.',flush=True)
    summary=task_summary(output,all_cases)
    print('FINISHED:',summary)
    from ah53runner.report import make_report
    make_report(output,all_cases,cfg)
    print('Report:',output/'report.html')
    return 130 if interrupted else (1 if summary['FAILED'] else 0)
if __name__=='__main__':
    sys.exit(main())
