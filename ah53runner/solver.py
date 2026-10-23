"""Real 2D XY FDTD; local validation uses Lumerical 2025 R2.

The installed version and raw monitors are recorded for every execution.
2023 R2.1 compatibility and metrology convergence are not implied.
"""
from __future__ import annotations
import importlib
import hashlib
import json
import math
import os
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path
import numpy as np
from .geometry import build_geometry, shapes_to_json
from .materials import (EFFECTIVE_ABSORPTION_K_THRESHOLD, fit_range_nm,
                        is_effectively_absorbing, resolve_materials, verify_material_databases)

C0=299792458.0
NM=1e-9


def load_lumapi(api_dir: str):
    if api_dir:
        p=Path(api_dir).expanduser().resolve()
        if not p.exists() or not (p/'lumapi.py').is_file():
            raise RuntimeError(f'Cannot find lumapi.py at {p}. Configure lumerical_api_python.')
        if str(p) not in sys.path:
            sys.path.insert(0,str(p))
    try:
        return importlib.import_module('lumapi')
    except ImportError as exc:
        raise RuntimeError('Cannot import lumapi. Set lumerical_api_python to a folder containing lumapi.py.') from exc


def as1d(x):
    return np.asarray(x).squeeze().reshape(-1)


def num_1(x):
    a=np.asarray(x).reshape(-1)
    if len(a)==0:
        raise ValueError('Empty value returned from Lumerical.')
    return float(np.real(a[0]))


def set_props(fdtd, props):
    for k,v in props.items():
        try:
            fdtd.set(k,v)
        except Exception:
            print('PROPERTY_FAILED',k,repr(v),flush=True)
            raise


@contextmanager
def local_cpu_resource(fdtd, threads=8):
    """Use one existing local CPU resource and restore its persistent settings."""
    row=None
    for candidate in range(1,int(fdtd.getresource('FDTD'))+1):
        if (str(fdtd.getresource('FDTD',candidate,'device type')).upper()=='CPU'
                and str(fdtd.getresource('FDTD',candidate,'hostname')).lower()
                in ('localhost','127.0.0.1','',os.environ.get('COMPUTERNAME','').lower())):
            row=candidate
            break
    if row is None:
        raise RuntimeError('No existing local CPU FDTD resource.')
    props=('job launching preset','processes','threads','capacity','active')
    snapshot={p:fdtd.getresource('FDTD',row,p) for p in props}
    try:
        for p,v in zip(props,('Local Computer','1',str(threads),'1','1')):
            fdtd.setresource('FDTD',row,p,v)
        observed={p:fdtd.getresource('FDTD',row,p) for p in props}
        observed['name']=fdtd.getresource('FDTD',row,'name')
        print('CPU_RESOURCE:',json.dumps(observed),flush=True)
        yield observed
    finally:
        for p in props:
            try:
                fdtd.setresource('FDTD',row,p,snapshot[p])
            except Exception as exc:
                print(f'WARNING resource restore {p}: {exc}',flush=True)


def add_rect(fdtd, s, zspan):
    if s.vertices_nm:
        fdtd.addpoly()
        vertices = np.array(s.vertices_nm, dtype=float)*NM
        set_props(fdtd,{'name':s.name,'x':0.0,'y':0.0,'vertices':vertices,
                        'z':0.0,'z span':zspan})
    else:
        fdtd.addrect()
        set_props(fdtd,{'name':s.name,'x':(s.x1_nm+s.x2_nm)/2*NM,
                        'x span':(s.x2_nm-s.x1_nm)*NM,
                        'y min':s.y1_nm*NM,'y max':s.y2_nm*NM,
                        'z':0.0,'z span':zspan})
    if s.material_name is not None:
        set_props(fdtd, {'material': s.material_name})
        if str(fdtd.get('material')) != s.material_name:
            raise ValueError(f'{s.name}: assigned material readback differs from {s.material_name!r}.')
    else:
        set_props(fdtd,{'material':'<Object defined dielectric>','index':float(s.index_n)})
        recorded = np.asarray(fdtd.get('index')).reshape(-1)
        if (str(fdtd.get('material')) != '<Object defined dielectric>' or len(recorded) != 1
                or not np.isclose(float(recorded[0]), s.index_n, rtol=1e-12, atol=0)):
            raise ValueError(f'{s.name}: constant material/index readback differs from requested index.')


def add_line_monitor(fdtd,name,y,P,lam):
    fdtd.adddftmonitor()
    set_props(fdtd,{'name':name,'monitor type':'Linear X','x':0.0,
                    'x span':P,'y':y,'z':0.0,'override global monitor settings':1,
                    'frequency points':1,'spatial interpolation':'specified position',
                    'sample spacing':'custom','custom frequency samples':np.array([C0/lam])})


def add_field_monitor(fdtd,P,sim,lam):
    fdtd.adddftmonitor()
    set_props(fdtd,{'name':'field_xy','monitor type':'2D Z-normal',
                    'x':0.0,'x span':P,'y min':sim['field_y_min_um']*1e-6,
                    'y max':sim['field_y_max_um']*1e-6,
                    'z':0.0,'override global monitor settings':1,'frequency points':1,
                    'sample spacing':'custom','custom frequency samples':np.array([C0/lam])})


def build_model(fdtd, shapes, top_nm, cfg, capture_field):
    sim=cfg['sim']
    P=cfg['period_nm']*NM
    lam=cfg['wavelength_nm']*NM
    fit_lo, fit_hi = fit_range_nm(cfg)
    y_min=sim['y_min_um']*1e-6
    y_max=sim['y_max_um']*1e-6
    src=sim['source_y_um']*1e-6
    inc1=sim['incident1_y_um']*1e-6
    inc2=sim['incident2_y_um']*1e-6
    mon_r=sim['reflection_y_um']*1e-6
    mon_t=sim['transmission_y_um']*1e-6
    ref=sim['reference_y_um']*1e-6
    if not (y_min<mon_t<0<=top_nm*NM<inc1<inc2<src<mon_r<ref<y_max):
        raise ValueError('Source/monitor/reference constraints fail; move or enlarge solver boundaries.')
    # Ensure substrate extends beyond y_min and lower PML.
    if y_min < shapes[0].y1_nm*NM:
        raise ValueError('Substrate must continue through lower PML.')
    for s in shapes:
        add_rect(fdtd,s,sim['stripe_z_span_um']*1e-6)
    fdtd.addfdtd()
    set_props(fdtd,{'dimension':'2D','index':1.0,
                    'x':0.0,'x span':P,'y min':y_min,'y max':y_max,
                    'x min bc':'Periodic','x max bc':'Periodic',
                    'y min bc':'PML','y max bc':'PML',
                    'pml profile':int(sim.get('pml_profile',1)),
                    'pml layers':int(sim.get('pml_layers',16)),
                    'simulation time':sim['simulation_time_fs']*1e-15,
                    'auto shutoff min':1e-7,
                    'mesh accuracy':int(cfg['mesh']['mesh_accuracy'])})
    mesh=cfg['mesh']
    if mesh['mode']=='override':
        fdtd.addmesh()
        set_props(fdtd,{'name':'mesh_all','x':0.0,'x span':P,
                        'y min':sim['mesh_y_min_um']*1e-6,
                        'y max':sim['mesh_y_max_um']*1e-6,
                        'override x mesh':1,'dx':mesh['dx_nm']*NM,
                        'override y mesh':1,'dy':mesh['dy_nm']*NM})
    elif mesh['mode']!='auto':
        raise ValueError('mesh.mode must be auto or override')
    fdtd.addplane()
    set_props(fdtd,{'name':'source','injection axis':'y-axis',
                    'direction':'backward','plane wave type':'Bloch/periodic',
                    'x':0.0,'x span':P,'y':src,
                    'angle theta':0,'polarization angle':90,
                    'wavelength start':fit_lo*NM,
                    'wavelength stop':fit_hi*NM})
    fdtd.setglobalmonitor('use source limits',False)
    fdtd.setglobalmonitor('wavelength center',lam)
    fdtd.setglobalmonitor('wavelength span',0.0)
    add_line_monitor(fdtd,'R',mon_r,P,lam)
    add_line_monitor(fdtd,'T',mon_t,P,lam)
    add_line_monitor(fdtd,'INC1',inc1,P,lam)
    add_line_monitor(fdtd,'INC2',inc2,P,lam)
    if capture_field:
        add_field_monitor(fdtd,P,sim,lam)
    return {'ref':ref,'r':mon_r,'t':mon_t,'inc1':inc1,'inc2':inc2,'P':P,'lam':lam}


def fld(fdtd,monitor,component):
    return as1d(fdtd.getdata(monitor,component)).astype(np.complex128)


def source_flux(fdtd, name):
    return float(abs(num_1(fdtd.transmission(name))))


def mean_complex(arr):
    return np.mean(as1d(arr))


def proj_fourier(x,field,period,mm):
    x=as1d(x).astype(float)
    field=as1d(field).astype(np.complex128)
    if len(x)!=len(field) or len(x)<3:
        raise ValueError('Monitor data x/E shape mismatch or too few samples.')
    if np.any(np.diff(x)<=0):
        idx=np.argsort(x)
        x,field=x[idx],field[idx]
    if np.any(np.diff(x)<=0):
        raise ValueError('Duplicate monitor x samples.')
    # Close the periodic interval, including the final cell for cell-centre
    # samples. Nonuniform samples use periodic trapezoids.
    if x[-1]-x[0] > period*(1+1e-6):
        raise ValueError('Monitor samples exceed one period.')
    if x[-1]-x[0] < period*(1-1e-9):
        x=np.r_[x,x[0]+period]
        field=np.r_[field,field[0]]
    integrand=field*np.exp(-1j*(2*math.pi*mm/period)*x)
    trap=np.trapezoid if hasattr(np,'trapezoid') else np.trapz
    return trap(integrand,x)/period


def retrieve_nearfield(fdtd, case_dir, pol, incident_amplitude):
    x=as1d(fdtd.getdata('field_xy','x')).real
    y=as1d(fdtd.getdata('field_xy','y')).real
    E2=None
    for comp in ('Ex','Ey','Ez'):
        dat=np.asarray(fdtd.getdata('field_xy',comp)).squeeze()
        if dat.shape != (len(x),len(y)):
            if dat.shape == (len(y),len(x)):
                dat=dat.T
            else:
                raise ValueError(f'Nearfield shape {dat.shape} vs x={len(x)},y={len(y)}')
        contrib=np.abs(dat)**2
        E2=contrib if E2 is None else E2+contrib
    np.savez_compressed(case_dir/f'nearfield_{pol}.npz',x_um=x*1e6,y_um=y*1e6,
                        E2=E2/abs(incident_amplitude)**2)


def run_case(case:dict, case_dir:Path, api_path:str, visible:bool=False)->dict:
    lumapi=load_lumapi(api_path)
    cfg=case['model']
    verify_material_databases(cfg)
    shapes,top_nm,meta=build_geometry(cfg)
    metadata={'case_id':case['id'],'digest':case['digest'],
              'solver_fingerprint':case.get('solver_fingerprint'),
              'group':case['group'],'parameter':case['parameter'],
              'value':case['value'],'geometry':meta,'physical_model':'2D lossless dielectric XY',
              'jones_phase_status':'NOT CALIBRATED AGAINST FRESNEL; research data only',
              'cross_terms_source':'analytic_by_2d_symmetry',
              'basis':{'input_s':'Ez','input_p':'+Ex',
                       'output_s':'Ez','output_p':'(cos(theta),-sin(theta),0)'},
              'reference_y_um':cfg['sim']['reference_y_um']}
    (case_dir/'geometry.json').write_text(json.dumps({'shapes':shapes_to_json(shapes),'info':meta},indent=2),encoding='utf-8')
    with lumapi.FDTD(hide=(not visible)) as fdtd, local_cpu_resource(fdtd) as resource:
        metadata['lumerical_version']=fdtd.version()
        metadata['resource']=resource
        materials = resolve_materials(fdtd, cfg)
        metadata['materials'] = materials
        absorbing = any(is_effectively_absorbing(role) for role in materials['roles'].values())
        if absorbing:
            metadata['physical_model'] = '2D passive isotropic dispersive absorbing XY'
        elif any(role['kind'] == 'database' for role in materials['roles'].values()):
            metadata['physical_model'] = '2D passive isotropic dispersive XY'
        material_file = case_dir/'material_resolution.json'
        material_file.write_text(json.dumps(materials, indent=2, allow_nan=False), encoding='utf8')
        metadata['material_resolution_sha256'] = hashlib.sha256(material_file.read_bytes()).hexdigest()
        coords=build_model(fdtd,shapes,top_nm,cfg,case.get('capture_field',False))
        metadata['power_monitor_planes'] = {
            'reflection_y_um': coords['r']*1e6,
            'transmission_y_um': coords['t']*1e6,
            'transmission_medium': 'substrate',
            'transmission_definition': 'Downward power flux at the stated internal substrate plane; includes attenuation above this plane.',
            'residual_definition': '1-R-T_plane; unaccounted flux between monitors, including absorption and numerical error; not independently measured absorption.',
        }
        fdtd.save(str((case_dir/'AH53_model.fsp').resolve()))
        structure_names=[s.name for s in shapes]

        def toggle_geometry(on):
            fdtd.switchtolayout()
            for name in structure_names:
                fdtd.setnamed(name,'enabled',int(bool(on)))

        def pol_and_run(angle,label):
            fdtd.switchtolayout()
            fdtd.setnamed('source','polarization angle',angle)
            print('FDTD_START',label,flush=True)
            started=time.monotonic()
            fdtd.run('FDTD','CPU',resource['name'])
            engine_logs=case_dir/'engine_logs'
            engine_logs.mkdir(exist_ok=True)
            for solver_log in case_dir.glob('AH53_model*.log'):
                shutil.copy2(solver_log,engine_logs/f'{label}_{solver_log.name}')
            raw={}
            for mon in ('R','T','INC1','INC2'):
                for key in ('x','y','z','f','Ex','Ey','Ez','Hx','Hy','Hz'):
                    raw[mon+'__'+key]=np.asarray(fdtd.getdata(mon,key))
                raw[mon+'__transmission']=np.asarray(fdtd.transmission(mon))
                freq=as1d(raw[mon+'__f']).real
                if len(freq)!=1 or not np.isclose(C0/freq[0],coords['lam'],rtol=1e-8,atol=0):
                    raise ValueError(f'{label}/{mon}: requested frequency was not recorded.')
            raw['mesh__x']=np.asarray(fdtd.getresult('FDTD','x'))
            raw['mesh__y']=np.asarray(fdtd.getresult('FDTD','y'))
            np.savez_compressed(case_dir/f'raw_{label}.npz',**raw)
            print('FDTD_END',label,'elapsed_s',round(time.monotonic()-started,3),flush=True)

        # Ref s, ref p: no sample, air only.
        toggle_geometry(False)
        pol_and_run(90,'reference_s')
        fs=as1d(fdtd.getdata('INC1','f')).real
        lam_selected=C0/fs[0]
        if len(fs)!=1 or not np.isclose(lam_selected,coords['lam'],rtol=1e-8,atol=0):
            raise ValueError(f'Monitor wavelength {lam_selected/NM} nm differs from requested {coords["lam"]/NM} nm.')
        s1=mean_complex(fld(fdtd,'INC1','Ez'))
        s2=mean_complex(fld(fdtd,'INC2','Ez'))
        ref_s_R=fld(fdtd,'R','Ez')
        ref_xs=as1d(fdtd.getdata('R','x')).real
        reference_R_s=source_flux(fdtd,'R')
        angle_step=np.angle(s2*np.conj(s1))
        if abs(angle_step)<1e-5 or abs(angle_step)>2.8:
            raise ValueError(f'Cannot determine incident spatial phase convention: {angle_step}')
        sign_in=1 if angle_step>0 else -1
        if sign_in != -1:
            raise ValueError(f'Unsupported Lumerical phase sign {sign_in}; p basis not calibrated.')
        k0=2*math.pi/lam_selected
        s_inc_ref=s1*np.exp(1j*sign_in*k0*(coords['ref']-coords['inc1']))
        if abs(s_inc_ref)<1e-8: raise ValueError('Reference s field nearly zero.')
        pol_and_run(0,'reference_p')
        p1=mean_complex(fld(fdtd,'INC1','Ex'))
        ref_p_R_x=fld(fdtd,'R','Ex')
        ref_p_R_y=fld(fdtd,'R','Ey')
        ref_xp=as1d(fdtd.getdata('R','x')).real
        reference_R_p=source_flux(fdtd,'R')
        p_inc_ref=p1*np.exp(1j*sign_in*k0*(coords['ref']-coords['inc1']))
        if abs(p_inc_ref)<1e-8: raise ValueError('Reference p field nearly zero.')

        toggle_geometry(True)
        pol_and_run(90,'structure_s')
        xs=as1d(fdtd.getdata('R','x')).real
        E_s=fld(fdtd,'R','Ez')
        Rmon_s=source_flux(fdtd,'R')
        Tmon_s=source_flux(fdtd,'T')
        signed_R_s=num_1(fdtd.transmission('R'))
        signed_T_s=num_1(fdtd.transmission('T'))
        nearfield_warnings=[]
        if case.get('capture_field',False):
            try:retrieve_nearfield(fdtd,case_dir,'s',s_inc_ref)
            except Exception as exc:
                nearfield_warnings.append('s nearfield extraction: '+repr(exc))
                print('WARNING:',nearfield_warnings[-1],flush=True)
        pol_and_run(0,'structure_p')
        xp=as1d(fdtd.getdata('R','x')).real
        E_p_x=fld(fdtd,'R','Ex')
        E_p_y=fld(fdtd,'R','Ey')
        Rmon_p=source_flux(fdtd,'R')
        Tmon_p=source_flux(fdtd,'T')
        signed_R_p=num_1(fdtd.transmission('R'))
        signed_T_p=num_1(fdtd.transmission('T'))
        if case.get('capture_field',False):
            try:retrieve_nearfield(fdtd,case_dir,'p',p_inc_ref)
            except Exception as exc:
                nearfield_warnings.append('p nearfield extraction: '+repr(exc))
                print('WARNING:',nearfield_warnings[-1],flush=True)

        maxm=math.floor((cfg['period_nm']*NM)/lam_selected-1e-12)
        orders=np.arange(-maxm,maxm+1,dtype=int)
        theta=np.rad2deg(np.arcsin(orders*lam_selected/coords['P']))
        rJ=np.zeros((len(orders),2,2),dtype=np.complex128)
        for idx,mm in enumerate(orders):
            u=mm*lam_selected/coords['P']
            ct=math.sqrt(max(0.0,1-u*u))
            ky=k0*ct
            # Outgoing upward field yR -> common reference y_ref.
            propagation=np.exp(-1j*sign_in*ky*(coords['ref']-coords['r']))
            s_out=proj_fourier(xs,E_s,coords['P'],mm)-proj_fourier(ref_xs,ref_s_R,coords['P'],mm)
            px=proj_fourier(xp,E_p_x,coords['P'],mm)-proj_fourier(ref_xp,ref_p_R_x,coords['P'],mm)
            py=proj_fourier(xp,E_p_y,coords['P'],mm)-proj_fourier(ref_xp,ref_p_R_y,coords['P'],mm)
            p_out=ct*px-u*py
            rJ[idx,0,0]=s_out*propagation/s_inc_ref
            rJ[idx,1,1]=p_out*propagation/p_inc_ref
            # Isotropic, z-invariant, nonconical: s/p do not mix.
        ct_vec=np.cos(np.deg2rad(theta))
        r_power=rJ*np.sqrt(ct_vec)[:,None,None]
        eta_s=ct_vec*np.sum(np.abs(rJ[:,:,0])**2,axis=1)
        eta_p=ct_vec*np.sum(np.abs(rJ[:,:,1])**2,axis=1)
        quality={'selected_lambda_nm':lam_selected/NM,'total_reflected_s':Rmon_s,
                 'total_reflected_p':Rmon_p,'total_transmitted_s':Tmon_s,
                 'total_transmitted_p':Tmon_p,'sum_eta_s':float(np.sum(eta_s)),
                 'sum_eta_p':float(np.sum(eta_p)),
                 'flux_decomposition_error_s':abs(Rmon_s-float(np.sum(eta_s))),
                 'flux_decomposition_error_p':abs(Rmon_p-float(np.sum(eta_p))),
                 'power_closure_s':Rmon_s+Tmon_s,
                 'power_closure_p':Rmon_p+Tmon_p,
                 'absorption_present':absorbing,
                 'absorption_k_threshold':EFFECTIVE_ABSORPTION_K_THRESHOLD,
                 'absorption_classification':'Raw k>0 always remains absorbing; only raw-lossless materials with fitted k<=threshold use the strict lossless closure check. Actual fitted k is retained.',
                 'maximum_fitted_k':max(role['k'] for role in materials['roles'].values()),
                 'energy_check_kind':'passive_flux_bound' if absorbing else 'lossless_flux_closure',
                 'energy_residual_s':1-Rmon_s-Tmon_s,
                 'energy_residual_p':1-Rmon_p-Tmon_p,
                 'signed_reflection_flux_s':signed_R_s,
                 'signed_reflection_flux_p':signed_R_p,
                 'signed_transmission_flux_s':signed_T_s,
                 'signed_transmission_flux_p':signed_T_p,
                 'incident_spatial_phase_sign':sign_in,
                 'air_reference_reflected_s':reference_R_s,
                 'air_reference_reflected_p':reference_R_p,
                 'nearfield_warnings':nearfield_warnings}
        np.savez_compressed(case_dir/'result.npz',orders=orders,theta_deg=theta,
                            r_E=rJ,r_power=r_power,eta_s=eta_s,eta_p=eta_p,
                            lambda_nm=np.array([lam_selected/NM]),
                            quality_json=np.array([json.dumps(quality)]))
        metadata['quality']=quality
        metadata['pml']={'profile':int(cfg['sim'].get('pml_profile',1)),
                         'layers':int(cfg['sim'].get('pml_layers',16))}
        metadata['reflection_power_note']='total_reflected is raw monitor flux; eta uses air-background-subtracted E. Compare only after the reference leakage gate passes.'
        metadata['result_sha256']=hashlib.sha256((case_dir/'result.npz').read_bytes()).hexdigest()
        verify_material_databases(cfg)
    (case_dir/'physics_metadata.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    return quality
