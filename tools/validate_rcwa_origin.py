"""Read real weak-scattering controls against absolute, analytic Born phases.

Three hypotheses and all tolerances are declared below before reading data.
There is no fitted offset/phase and no FDTD data are used. Raw data and existing
calibration certificates are never changed. This creates a diagnostic verdict,
not an RCWA export certificate.
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import math
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ah53runner import rcwa
from ah53runner.planning import load_config, plan

HYPOTHESES = ('native', 'half_cell', 'cell_origin_half_cell')
ORDERS = (-5, -2, -1, 0, 1, 2, 5)
TOLERANCES = {'amplitude_floor': 1e-7, 'complex_relative': 0.01,
              'phase_deg': 1., 'chi_halving_scaled_complex_relative': 0.005}


def born_reflection_s(model, orders):
    """TE first Born result at y_ref; physical q is in inverse nm.

    np.sinc(v)=sin(pi*v)/(pi*v), so longitudinal and transverse arguments
    intentionally differ by pi. The Green-function sign is positive i.
    """
    orders = np.asarray(orders, dtype=int)
    wavelength, period = float(model['wavelength_nm']), float(model['period_nm'])
    k0 = 2*np.pi/wavelength
    gx = 2*np.pi*orders/period
    q = np.sqrt(k0*k0-gx*gx)
    height, cd, pitch = (float(model[key]) for key in ('ridge_height_nm','ridge_cd_nm','child_pitch_nm'))
    chi = float(model['n_ridge'])**2-1.
    chi_m = chi*cd/period*np.sinc(orders*cd/period)*(1+2*np.cos(2*np.pi*orders*pitch/period))
    thickness = height*np.sinc((k0+q)*height/(2*np.pi))
    reference = 1000*float(model['sim']['reference_y_um'])
    return 1j*k0*k0/(2*q)*chi_m*thickness*np.exp(1j*(k0+q)*(reference-height/2))


def hypothesis_factor(model, settings, orders, hypothesis):
    rcwa.require(hypothesis in HYPOTHESES, 'Unknown transverse-origin hypothesis.')
    period = float(model['period_nm'])
    if hypothesis == 'native':
        return np.ones(len(orders),dtype=complex)
    half_cell = np.exp(-2j*np.pi*np.asarray(orders)/(2*settings['mesh_cells_x']))
    if hypothesis == 'half_cell':
        return half_cell
    x_min = -period/2
    return half_cell*np.exp(-2j*np.pi*np.asarray(orders)*x_min/period)


def _check(name,value,limit):
    return {'name':name,'value':float(value),'limit':float(limit),
            'status':'PASS' if math.isfinite(float(value)) and value<=limit else 'FAIL'}


def evaluate_origin(records):
    """Evaluate in memory after real-acquisition completeness checks."""
    results = {}
    for hypothesis in HYPOTHESES:
        checks, controls, by_mesh = [], [], {}
        for frozen,state,arrays in records:
            model,settings = frozen['case']['model'],frozen['settings']
            orders = np.asarray(ORDERS,dtype=int)
            positions = [int(np.flatnonzero(arrays['r_orders']==m)[0]) for m in orders]
            expected = born_reflection_s(model,orders)
            factor = hypothesis_factor(model,settings,orders,hypothesis)
            actual = arrays['r_E'][positions,0,0]*factor
            actual_p = arrays['r_E'][positions,1,1]*factor
            chi = float(model['n_ridge'])**2-1
            prefix = f'{settings["name"]}_n{model["n_ridge"]}'
            modes = []
            for i,m in enumerate(orders):
                observable = min(abs(expected[i]),abs(actual[i]))>=TOLERANCES['amplitude_floor']
                checks.append(_check(f'{prefix}_m{m}_observable',0 if observable else 1,0))
                if not observable:
                    modes.append({'order':int(m),'status':'UNRESOLVED','phase_difference_deg':None})
                    continue
                relative = abs(actual[i]-expected[i])/abs(expected[i])
                phase = float(np.rad2deg(np.angle(actual[i]*np.conj(expected[i]))))
                checks.append(_check(f'{prefix}_m{m}_complex_relative',relative,TOLERANCES['complex_relative']))
                checks.append(_check(f'{prefix}_m{m}_phase_deg',abs(phase),TOLERANCES['phase_deg']))
                ct = math.sqrt(1-(m*float(model['wavelength_nm'])/float(model['period_nm']))**2)
                modes.append({'order':int(m),'actual_s_real':float(actual[i].real),'actual_s_imag':float(actual[i].imag),
                    'born_s_real':float(expected[i].real),'born_s_imag':float(expected[i].imag),
                    'complex_relative_error':float(relative),'phase_difference_deg':phase,
                    'p_born_relative_diagnostic_only':float(abs(actual_p[i]-ct*expected[i])/abs(ct*expected[i]))})
            controls.append({'case_id':frozen['case']['id'],'mesh_cells_x':settings['mesh_cells_x'],
                             'n_ridge':model['n_ridge'],'chi':chi,'modes':modes})
            by_mesh.setdefault(settings['mesh_cells_x'],[]).append((chi,actual))
        for mesh,contrasts in sorted(by_mesh.items()):
            rcwa.require(len(contrasts)==2,'Each mesh must contain exactly two independently acquired contrasts.')
            contrasts.sort(key=lambda item:item[0])
            weak_chi,weak = contrasts[0]
            strong_chi,strong = contrasts[1]
            rcwa.require(abs(strong_chi/weak_chi-2)<1e-3,'Contrasts are not the declared chi-halving pair.')
            scaled_weak,scaled_strong = weak/weak_chi,strong/strong_chi
            denominator = np.abs(scaled_weak)
            rcwa.require(np.all(denominator>0),'Unresolved chi-scaled weak control.')
            error = float(np.max(np.abs(scaled_strong-scaled_weak)/denominator))
            checks.append(_check(f'{mesh}_chi_halving_scaled_complex_relative',error,
                                 TOLERANCES['chi_halving_scaled_complex_relative']))
        results[hypothesis] = {'status':'PASS' if all(c['status']=='PASS' for c in checks) else 'FAIL',
                               'checks':checks,'controls':controls}
    return results


def validate(config,path):
    evidence,records = [],[]
    report = {'schema_version':'1.0','status':'REFUSED','evaluated':False,
              'purpose':'Absolute transverse-origin diagnosis against analytic weak-scattering TE Born amplitudes; no fitted phase and no FDTD reference.',
              'hypotheses':list(HYPOTHESES),'tolerances':TOLERANCES,'primary_orders':list(ORDERS),
              'phase_status':'ORIGIN_DIAGNOSTIC_ONLY_NOT_A_CALIBRATION_CERTIFICATE',
              'backend_fingerprint':rcwa.backend_fingerprint(),'evidence':evidence,
              'evaluated_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec='seconds')}
    try:
        output = Path(config['output_dir'])
        if not output.is_absolute(): output=Path(path).resolve().parent/output
        levels = config['rcwa_levels']
        rcwa.require(len(levels)==2 and len({v['mesh_cells_x'] for v in levels})==2,'Exactly two independent declared meshes are required.')
        cases = plan(config)
        rcwa.require(len(cases)==2,'Exactly two weak-contrast cases are required.')
        values = sorted(float(c['model']['n_ridge']) for c in cases)
        rcwa.require(values==[1.00005,1.0001],'Only the predeclared n=1.00005/1.0001 pair is accepted.')
        baseline=None
        for case in cases:
            model=case['model']
            rcwa.require(model.get('geometry_kind','ah53')=='ah53' and model['n_substrate']==1
                         and model['ridge_height_nm']==100 and not model.get('films')
                         and model.get('ridge_shift_nm',0)==0
                         and model.get('ridge_center_offsets_nm',[0,0,0])==[0,0,0]
                         and not model.get('ridge_top_left_inset_nm',0) and not model.get('ridge_top_right_inset_nm',0),
                         'Born controls must be the declared bare symmetric 100nm ridges in air.')
            normalized=dict(model);normalized.pop('n_ridge')
            rcwa.require(baseline is None or baseline==normalized,'Weak controls change more than ridge index.')
            baseline=normalized
            for level in levels:
                folder=(output/case['id']/level['name']).resolve()
                frozen,state,datasets=rcwa.read_acquisition(folder)
                rcwa.require(frozen['case']==case and frozen['settings']==level,'Actual acquisition differs from current planned weak control.')
                rcwa.require(frozen['geometry']==rcwa.geometry_spec(model),'Actual frozen geometry is not the shared geometry.')
                arrays,_=rcwa.convert_native(datasets,model,level,'native')
                records.append((frozen,state,arrays))
                evidence.append({'folder':str(folder),'case_id':case['id'],'input_sha256':state['input_sha256'],
                    'native_status_sha256':rcwa.sha256(folder/'native_status.json'),
                    'backend_fingerprint':state['backend_fingerprint'],'lumerical_version':state['lumerical_version'],
                    'file_sha256':state['file_sha256']})
        rcwa.require(len({item['lumerical_version'] for item in evidence})==1,'Controls use different actual solver versions.')
        candidates=evaluate_origin(records)
        winners=[name for name,value in candidates.items() if value['status']=='PASS']
        report.update(evaluated=True,candidates=candidates,passing_hypotheses=winners,
                      status='PASS' if len(winners)==1 else 'FAIL',
                      interpretation='Exactly one declared origin hypothesis must satisfy all absolute Born and chi-linearity controls at both meshes. This does not update any export certificate.')
    except Exception as error:
        report['errors']=[f'{type(error).__name__}: {error}']
    destination=ROOT/'local_validation'
    destination.mkdir(exist_ok=True)
    (destination/'rcwa_origin_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')
    body=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>RCWA横向原点独立验收</title>',
          '<style>body{font:15px/1.65 system-ui;max-width:1200px;margin:30px auto;padding:0 20px}table{border-collapse:collapse;width:100%}td,th{padding:6px;border-bottom:1px solid #ddd;text-align:left}.FAIL{color:#b00}</style>',
          f'<h1>RCWA 横向原点诊断：{report["status"]}</h1>',
          '<p>真实四项弱散射控制对照绝对 TE Born 解析式。预声明 native、half_cell、cell_origin_half_cell 三候选；没有拟合相位、没有 FDTD 参照。此页不修改原始结果，也不是可直接导出系数的校准证书。</p>',
          '<p>m=0、±1、±2、±5；幅值floor=1e-7；复相对误差≤1%；相位误差≤1°；chi减半后的r/chi一致性≤0.5%。p结果仅作诊断。</p>',
          '<p><a href="rcwa_origin_validation.json">全部原始证据摘要、候选和逐阶数值</a></p>']
    for error in report.get('errors',[]): body.append('<p>'+html.escape(error)+'</p>')
    for name,result in report.get('candidates',{}).items():
        body.append('<h2>'+name+'：'+result['status']+'</h2><table><tr><th>检查</th><th>值</th><th>门限</th><th>状态</th></tr>')
        for item in result['checks']:
            body.append('<tr class="'+item['status']+'"><td>'+html.escape(item['name'])+'</td><td>'+f'{item["value"]:.9g}'+'</td><td>'+str(item['limit'])+'</td><td>'+item['status']+'</td></tr>')
        body.append('</table>')
    body.append('</html>')
    (destination/'rcwa_origin_validation.html').write_text('\n'.join(body),encoding='utf8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(ROOT/'configs'/'rcwa_origin_validation.json'))
    args=parser.parse_args()
    config,path=load_config(args.config)
    report=validate(config,path)
    print('RCWA ORIGIN DIAGNOSTIC:',report['status'],'evaluated=',report['evaluated'])
    for name,result in report.get('candidates',{}).items():print(name,result['status'])
    for error in report.get('errors',[]):print('REFUSED:',error)
    print('REPORT:',ROOT/'local_validation'/'rcwa_origin_validation.html')
    return 0 if report['status']=='PASS' else (2 if report['status']=='REFUSED' else 1)


if __name__=='__main__':
    raise SystemExit(main())
