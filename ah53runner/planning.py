"""Create independent single-variable cases with reproducible fingerprints."""
from __future__ import annotations
import copy
import hashlib
import json
import re
from pathlib import Path
from .geometry import build_geometry
from .materials import freeze_material_databases, validate_material_config

def canonical_hash(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf8')).hexdigest()[:12]

def safe(x):
    return re.sub(r'[^a-zA-Z0-9_.-]+','_',str(x))[:64]

def apply_patch(obj:dict, dotted:str, value):
    keys=dotted.split('.')
    target=obj
    for key in keys[:-1]:
        target=target[int(key)] if isinstance(target,list) else target[key]
    last=keys[-1]
    if isinstance(target,list): target[int(last)]=value
    else: target[last]=value


def deepmerge(base, changes):
    for key,val in changes.items():
        if key in base and isinstance(base[key],dict) and isinstance(val,dict):
            deepmerge(base[key],val)
        else: base[key]=copy.deepcopy(val)
    return base


def check_model(model):
    validate_material_config(model)
    if model['wavelength_nm']<=0:
        raise ValueError('Wavelength must be >0.')
    mesh=model['mesh']
    if mesh['mode']=='override':
        if mesh['dx_nm']<=0 or mesh['dy_nm']<=0:
            raise ValueError('Mesh override dx/dy must be >0.')
    elif mesh['mode']!='auto': raise ValueError('Invalid mesh.mode')
    if model.get('geometry_kind', 'ah53') == 'ah53' and model['period_nm']<=model['ridge_cd_nm']:
        raise ValueError('Period must exceed CD')
    shapes,top,summary=build_geometry(model)
    sim=model['sim']
    if not (sim['y_min_um']<sim['transmission_y_um']<0<=top/1000<sim['incident1_y_um']<sim['incident2_y_um']<sim['source_y_um']<sim['reflection_y_um']<sim['reference_y_um']<sim['y_max_um']):
        raise ValueError('Geometry top overlaps incident/source/reflect reference planes.')
    if sim['y_min_um']< -1.6:
        raise ValueError('Substrate bottom (-1.6 um) must extend beyond y_min.')
    return summary


def expand_values(exp):
    """Accept explicit values or a closed numeric start/stop/count or start/stop/step."""
    if 'values' in exp:
        return exp['values']
    if all(x in exp for x in ('start','stop','count')):
        count=int(exp['count'])
        if count<1: raise ValueError('count must be >=1')
        if count==1:return [exp['start']]
        return [round(float(exp['start'])+(float(exp['stop'])-float(exp['start']))*i/(count-1),10)
                for i in range(count)]
    if all(x in exp for x in ('start','stop','step')):
        start,stop,step=map(float,(exp['start'],exp['stop'],exp['step']))
        if step==0 or (stop-start)*step<0:raise ValueError('step has wrong sign')
        n=int((stop-start)/step+1e-9)+1
        if n>10000:raise ValueError('Refusing more than 10000 cases in one sweep')
        return [round(start+i*step,10) for i in range(n)]
    raise ValueError('Experiment requires values, start/stop/count, or start/stop/step')

def plan(cfg: dict):
    base=cfg['base_model']
    check_model(base)
    cases=[]
    for exp in cfg.get('experiments',[]):
        if exp.get('enabled',True) is False: continue
        group=safe(exp['name'])
        values=expand_values(exp)
        if len(values)==0: raise ValueError(f'{group}: no values')
        previous=None
        for index,value in enumerate(values):
            c=deepmerge(copy.deepcopy(base),exp.get('overrides',{}))
            apply_patch(c,exp['parameter'],value)
            if exp['parameter']=='mesh.mesh_accuracy' and c['mesh']['mode']=='override':
                raise ValueError(f'{group}: mesh accuracy sweep would be hidden by mesh override; set mode auto.')
            if exp['parameter'] in ('mesh.dx_nm','mesh.dy_nm') and c['mesh']['mode']!='override':
                raise ValueError(f'{group}: explicit mesh dx/dy sweep requires override mode.')
            check_model(c)
            if previous is not None:
                # single-variable case invariant: after group override, only swept path changes.
                other=copy.deepcopy(c)
                apply_patch(other,exp['parameter'],'__SWEEP_PARAMETER__')
                if other!=previous:
                    raise ValueError(f'{group}: another variable changed between points.')
            else:
                previous=copy.deepcopy(c)
                apply_patch(previous,exp['parameter'],'__SWEEP_PARAMETER__')
            state={'name':group,'index':index,'parameter':exp['parameter'],'value':value,'model':c}
            digest=canonical_hash(state)
            case={'id':f'{group}_{index:03}_{safe(value)}_{digest}',
                  'digest':digest,'group':group,'parameter':exp['parameter'],
                  'value':value,'model':c,
                  'note':exp.get('note','')}
            cases.append(case)
    capture=int(cfg.get('capture_field_count_per_group',3))
    if capture:
        for group in sorted(set(c['group'] for c in cases)):
            seq=[c for c in cases if c['group']==group]
            if capture>=len(seq): chosen=set(range(len(seq)))
            elif capture==1:chosen={len(seq)//2}
            else: chosen={round(i*(len(seq)-1)/(capture-1)) for i in range(capture)}
            for ii,c in enumerate(seq):
                c['capture_field']=ii in chosen
    return cases


def load_config(path):
    p=Path(path).expanduser().resolve()
    data=json.loads(p.read_text(encoding='utf8'))
    if data.get('schema_version')!='2.0':
        raise ValueError('Expected schema_version 2.0')
    if 'material_databases' in data.get('base_model', {}):
        raise ValueError('Put material_databases at the top level, next to base_model; model records are reserved for frozen inputs.')
    if 'material_databases' in data:
        records=freeze_material_databases(data['material_databases'],p.parent)
        if records:
            data['base_model']['material_databases']=records
    return data,p
