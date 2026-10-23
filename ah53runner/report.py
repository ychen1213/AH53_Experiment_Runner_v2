"""Generate reproducible, label-safe reports from completed *actual* solver results.

No mocked / synthetic results are ever included in the final report.
"""
from __future__ import annotations
import csv
import html
import json
import math
from collections import defaultdict
from pathlib import Path
import numpy as np
from .artifacts import load_artifacts, strict_json_loads
from .validation import DEFAULT_TOLERANCES


def _status(path):
    try:
        value=strict_json_loads(path.read_text(encoding='utf8'))
        return value if isinstance(value,dict) else {'status':'PENDING'}
    except (OSError,ValueError):return {'status':'PENDING'}


def _make_plot(x,data,labels,title,xlabel,ylabel,filename):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(8,4.5))
    anyvalid=False
    for y,label in zip(data,labels):
        if np.any(np.isfinite(y)):
            ax.plot(x,y,'-o',markersize=4,label=label)
            anyvalid=True
    if anyvalid: ax.legend(fontsize=8,ncol=2)
    ax.set(xlabel=xlabel,ylabel=ylabel,title=title)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(filename,dpi=145)
    plt.close(fig)


def _phase_floor(r,component=(0,0),absolute=1e-8,relative=1e-3):
    amplitudes=np.abs(r['r_E'][:,component[0],component[1]])
    return max(float(absolute),float(relative)*float(np.max(amplitudes)))


def _phase_pair(r,m,component=(0,0),absolute=1e-8,relative=1e-3):
    idxp=np.where(r['orders']==m)[0]
    idxn=np.where(r['orders']==-m)[0]
    if len(idxp)==0 or len(idxn)==0: return float('nan')
    a=r['r_E'][idxp[0],component[0],component[1]]; b=r['r_E'][idxn[0],component[0],component[1]]
    if min(abs(a),abs(b)) <= _phase_floor(r,component,absolute,relative):
        return float('nan')
    return float(np.angle(a*np.conj(b)))


def _unwrapped_with_gaps(phases):
    vals=np.asarray(phases,dtype=float)
    out=np.full_like(vals,np.nan)
    seg=[]
    for i,v in enumerate(vals):
        if np.isfinite(v):seg.append(i)
        else:
            if seg:
                out[seg]=np.unwrap(vals[seg])*180/math.pi
                seg=[]
    if seg:out[seg]=np.unwrap(vals[seg])*180/math.pi
    return out


def _strict_power_warnings(case_id,quality):
    """Expose physical failures without hiding complete, measured solver data."""
    warnings=[]
    leakage_limit=DEFAULT_TOLERANCES['reference_leakage_power_max']
    relative_limit=DEFAULT_TOLERANCES['flux_decomposition_relative']
    reflection_floor=DEFAULT_TOLERANCES['flux_reflection_floor']
    for pol in ('s','p'):
        key=f'air_reference_reflected_{pol}'
        leakage=quality.get(key)
        if (not isinstance(leakage,(int,float)) or not math.isfinite(leakage) or leakage<0):
            reason='missing' if key not in quality else 'invalid'
            warnings.append((case_id,f'Physics check unavailable: {reason} measured {key}',None))
        elif leakage>leakage_limit:
            warnings.append((case_id,f'Air-reference leakage {pol} > {leakage_limit:g}',leakage))
        # Required common-power fields have already passed artifact validation.
        relative=quality[f'flux_decomposition_error_{pol}']/max(
            quality[f'total_reflected_{pol}'],reflection_floor)
        if relative>relative_limit:
            warnings.append((case_id,f'Fourier/raw-R relative discrepancy {pol} > {relative_limit:g}',relative))
    return warnings


def _nearfields(figdir,cases,records):
    """Global colour scale for each group+polarization; individual figures."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    items=[]
    for c in cases:
        record=records.get(c['id'],{})
        if record.get('state')!='DONE':continue
        for pol,d in record.get('nearfields',{}).items():
            items.append((c,pol,d['x_um'],d['y_um'],d['E2']))
    images=[]
    for pol in ('s','p'):
        section=[x for x in items if x[1]==pol]
        if not section:continue
        vmax=max(float(np.nanmax(q[-1])) for q in section)
        if vmax<=0:continue
        for c,_,x,y,data in section:
            fig,ax=plt.subplots(figsize=(8,3))
            im=ax.pcolormesh(x,y,data.T,shading='auto',vmin=0,vmax=vmax)
            ax.set(title=f"{c['group']}: {c['value']} | |E|²/|E_inc|² ({pol})",xlabel='X (µm)',ylabel='Y (µm)')
            fig.colorbar(im,ax=ax,label='|E|²/|E_inc|²')
            fig.tight_layout()
            save=figdir/f"field_{c['id']}_{pol}.png"
            fig.savefig(save,dpi=140)
            plt.close(fig)
            images.append(save)
    return images


def make_report(output:Path,cases:list,cfg:dict):
    output=Path(output)
    figdir=output/'figures'
    figdir.mkdir(parents=True,exist_ok=True)
    rows=[]
    report_records={}
    qc=[]
    absolute_floor=float(cfg.get('phase_min_amplitude',1e-8))
    relative_floor=float(cfg.get('phase_relative_amplitude_floor',1e-3))
    for c in cases:
        folder=output/c['id']
        status=_status(folder/'status.json')
        state=status.get('status','PENDING')
        if state=='DONE' and not (folder/'result.npz').exists():state='FAILED'
        rec={'case':c,'state':state,'folder':folder,'status':status}
        if state=='DONE':
            try:
                verified=load_artifacts(folder,c)
                r=verified['result']
                case_rows=[]
                case_qc=[]
                for i,m in enumerate(r['orders']):
                    row={'case_id':c['id'],'group':c['group'],'parameter':c['parameter'],
                         'value':c['value'],'m':int(m),'theta_deg':float(r['theta_deg'][i]),
                         'eta_s':float(r['eta_s'][i]),'eta_p':float(r['eta_p'][i])}
                    for name,(a,b) in {'rss':(0,0),'rsp':(0,1),'rps':(1,0),'rpp':(1,1)}.items():
                        z=r['r_E'][i,a,b]
                        row[name+'_real']=float(z.real)
                        row[name+'_imag']=float(z.imag)
                        row[name+'_abs']=float(abs(z))
                        floor=_phase_floor(r,(a,b),absolute_floor,relative_floor)
                        row[name+'_phase_deg']=float(np.angle(z)*180/math.pi) if abs(z)>floor else None
                        row[name+'_source']=('fdtd_fields' if a==b else
                            verified['metadata'].get('cross_terms_source','UNSPECIFIED'))
                    case_rows.append(row)
                q=verified['quality']
                case_qc.extend(_strict_power_warnings(c['id'],q))
                for pol in ('s','p'):
                    if abs(q[f'power_closure_{pol}']-1)>float(cfg.get('closure_warn_above',0.05)):
                        case_qc.append((c['id'],f'Power closure {pol}',q[f'power_closure_{pol}']))
                    if q[f'flux_decomposition_error_{pol}']>float(cfg.get('flux_warn_above',0.03)):
                        case_qc.append((c['id'],f'Fourier/monitor discrepancy {pol}',q[f'flux_decomposition_error_{pol}']))
                # These are QA flags only, not a claim of true physics convergence.
                if (c['model'].get('ridge_shift_nm',0)==0 and
                    c['model'].get('ridge_center_offsets_nm',[0,0,0])==[0,0,0] and
                    c['model'].get('ridge_top_left_inset_nm',0)==c['model'].get('ridge_top_right_inset_nm',0)):
                    phase_pair=_phase_pair(r,1,absolute=absolute_floor,relative=relative_floor)
                    if np.isfinite(phase_pair) and abs(phase_pair)>0.05:
                        case_qc.append((c['id'],'Symmetry ±1 phase (deg)',phase_pair*180/math.pi))
                rec.update(verified)
                rows.extend(case_rows)
                qc.extend(case_qc)
            except Exception as ex:
                rec['state']='RESULT_READ_ERROR'
                rec['read_error']=repr(ex)
        report_records[c['id']]=rec
    cols=['case_id','group','parameter','value','m','theta_deg','eta_s','eta_p']
    for name in ('rss','rsp','rps','rpp'):
        cols += [name+suffix for suffix in ('_real','_imag','_abs','_phase_deg','_source')]
    with (output/'coefficients_long.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols);w.writeheader();w.writerows(rows)
    with (output/'quality_warnings.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['case_id','test','value']);w.writerows(qc)
    with (output/'status_overview.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['case_id','group','parameter','value','status','elapsed_seconds','log','error'])
        for c in cases:
            rec=report_records[c['id']]
            w.writerow([c['id'],c['group'],c['parameter'],c['value'],rec['state'],
                        rec['status'].get('elapsed_s',''),str((output/c['id']/'worker.log').relative_to(output)),
                        rec.get('read_error') or rec['status'].get('error','')])
    groups=defaultdict(list)
    for c in cases:groups[c['group']].append(c)
    images=[]
    for group,cc in groups.items():
        completed=[(c,report_records[c['id']]['result']) for c in cc if report_records[c['id']]['state']=='DONE' and 'result' in report_records[c['id']]]
        if not completed:continue
        completed.sort(key=lambda x:float(x[0]['value']))
        x=np.array([float(c['value']) for c,r in completed])
        # m=0,1,5 are usually informative for AH53 (m=5 corresponds to sub-pitch first order).
        interesting=[m for m in (0,1,5) if any(m in r['orders'] for c,r in completed)]
        for component,ij in (('rss',(0,0)),('rpp',(1,1))):
            curves=[]
            labels=[]
            for m in interesting:
                y=[]
                for c,r in completed:
                    idx=np.where(r['orders']==m)[0]
                    y.append(abs(r['r_E'][idx[0],*ij]) if idx.size else np.nan)
                curves.append(np.asarray(y,dtype=float));labels.append(f'm={m}')
            filename=figdir/f'{group}_{component}_amplitude.png'
            _make_plot(x,curves,labels,group+' | '+component+' magnitude',
                       cc[0]['parameter'],'|r_E| (not max-normalized)',filename)
            images.append(filename)
        pair_curves=[];pair_labels=[]
        for pol,component in (('ss',(0,0)),('pp',(1,1))):
            for m in (1,5):
                y=_unwrapped_with_gaps([_phase_pair(r,m,component,absolute_floor,relative_floor) for c,r in completed])
                pair_curves.append(y);pair_labels.append(f'{pol} ±{m}')
        filename=figdir/f'{group}_plus_minus_phase.png'
        _make_plot(x,pair_curves,pair_labels,group+' | ±m relative phase',
                   cc[0]['parameter'],'Phase difference (deg), unwrapped',filename)
        images.append(filename)
        y_eta=[]; lab=[]
        for pol in ('s','p'):
            for m in (1,5):
                y=[]
                for c,r in completed:
                    idx=np.where(r['orders']==m)[0]
                    y.append(float(r['eta_'+pol][idx[0]]) if idx.size else np.nan)
                y_eta.append(y);lab.append(f'm={m}, {pol}')
        filename=figdir/f'{group}_reflected_power.png'
        _make_plot(x,y_eta,lab,group+' | reflected power fractions',
                   cc[0]['parameter'],'R_m (incident-power normalized)',filename)
        images.append(filename)
        # Complex-plane trajectory, m=5 for rss and rpp.
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,ax=plt.subplots(figsize=(6,5))
        for pol,(a,b) in (('rss',(0,0)),('rpp',(1,1))):
            y=[]
            for c,r in completed:
                idx=np.where(r['orders']==5)[0]
                y.append(r['r_E'][idx[0],a,b] if idx.size else complex(np.nan,np.nan))
            v=np.asarray(y)
            ax.plot(v.real,v.imag,'-o',label=f'{pol}, m=5')
        ax.set(xlabel='Re(r_E)',ylabel='Im(r_E)',title=group+' | m=5 complex plane')
        ax.set_aspect('equal',adjustable='datalim');ax.grid(alpha=.3);ax.legend()
        fig.tight_layout()
        filename=figdir/f'{group}_complex_plane.png'
        fig.savefig(filename,dpi=140);plt.close(fig);images.append(filename)
        # Mesh convergence must compare the complex r_E, not just Rtotal.
        if cc[0]['parameter'] in ('mesh.dx_nm','mesh.dy_nm','mesh.mesh_accuracy') and len(completed)>1:
            if cc[0]['parameter']=='mesh.mesh_accuracy':
                ref_c,ref_r=max(completed,key=lambda pair:float(pair[0]['value']))
            else:
                ref_c,ref_r=min(completed,key=lambda pair:float(pair[0]['value']))
            errs=[]
            for c,r in completed:
                common=np.intersect1d(r['orders'],ref_r['orders'])
                common=np.array([m for m in common if abs(m)<=8])
                v=[]
                vref=[]
                for m in common:
                    i=np.where(r['orders']==m)[0][0]
                    j=np.where(ref_r['orders']==m)[0][0]
                    v.extend([r['r_E'][i,0,0],r['r_E'][i,1,1]])
                    vref.extend([ref_r['r_E'][j,0,0],ref_r['r_E'][j,1,1]])
                v=np.asarray(v); vref=np.asarray(vref)
                rel=float(np.linalg.norm(v-vref)/max(np.linalg.norm(vref),1e-12))
                errs.append(rel)
            filename=figdir/f'{group}_complex_convergence.png'
            _make_plot(x,[np.array(errs)],['relative complex error'],
                       group+' | Jones convergence vs finest case',cc[0]['parameter'],
                       '||r-r_ref||₂ / ||r_ref||₂, |m|≤8',filename)
            images.append(filename)
            with (output/f'{group}_convergence.csv').open('w',newline='',encoding='utf-8-sig') as f:
                writer=csv.writer(f)
                writer.writerow(['case','parameter_value','relative_complex_error','reference_case'])
                for (c,_),err in zip(completed,errs):
                    writer.writerow([c['id'],c['value'],err,ref_c['id']])
        # Rigid shift theorem: check Δφ_m = -4πm Δx/P (mod 2π).
        if cc[0]['parameter']=='ridge_shift_nm' and len(completed)>1:
            reference=min(completed,key=lambda pair:abs(float(pair[0]['value'])))
            c0,r0=reference
            with (output/f'{group}_translation_validation.csv').open('w',newline='',encoding='utf-8-sig') as f:
                writer=csv.writer(f)
                writer.writerow(['case','shift_nm','order','measured_delta_deg','expected_delta_deg','wrapped_error_deg'])
                for c,r in completed:
                    dx=float(c['value'])-float(c0['value'])
                    for m in (1,5):
                        cur=_phase_pair(r,m,absolute=absolute_floor,relative=relative_floor)
                        ref=_phase_pair(r0,m,absolute=absolute_floor,relative=relative_floor)
                        if not np.isfinite(cur) or not np.isfinite(ref):continue
                        delta=np.angle(np.exp(1j*(cur-ref)))
                        expected=-4*math.pi*m*dx/c['model']['period_nm']
                        diff=np.angle(np.exp(1j*(delta-expected)))
                        writer.writerow([c['id'],dx,m,delta*180/math.pi,expected*180/math.pi,diff*180/math.pi])
                        if abs(diff*180/math.pi)>0.5:
                            qc.append((c['id'],f'Translation theorem residual m={m} (deg)',diff*180/math.pi))
        images.extend(_nearfields(figdir,cc,report_records))
    # Re-write warnings once post-hoc convergence/translation checks have run.
    with (output/'quality_warnings.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['case_id','test','value']);w.writerows(qc)
    body=[]
    body.append('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>AH53 Batch Report</title>')
    body.append('<style>body{font:15px/1.65 system-ui,sans-serif;max-width:1100px;margin:30px auto;padding:0 20px;color:#222}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #ddd;padding:7px;text-align:left}img{max-width:96%;border:1px solid #ccc;margin:12px 0}code{background:#f3f3f3;padding:2px 5px}small{color:#666}</style>')
    body.append('<h1>AH53 Experiment Runner: 计算报告</h1>')
    body.append('<p><b>注意：</b>只展示通过配置摘要、源码指纹、文件校验和与完整性检查的本地求解结果；这些检查不代表电磁计算已经收敛或校准。<br>对角Jones系数由FDTD场投影得到；二维解耦模型的交叉项按对称性设零，CSV的source列记录来源。Jones 复相位在通过 Fresnel 与相位参考验证前均属于<b>研究性结果</b>。</p>')
    body.append('<p><a href="coefficients_long.csv">完整复矩阵数据（CSV）</a>　<a href="status_overview.csv">任务状态</a>　<a href="quality_warnings.csv">质量预警</a></p>')
    body.append('<h2>任务汇总</h2><table><tr><th>组</th><th>已完成/总数</th><th>参数</th></tr>')
    for group,cc in groups.items():
        ndone=sum(report_records[c['id']]['state']=='DONE' and 'result' in report_records[c['id']] for c in cc)
        body.append(f'<tr><td>{html.escape(group)}</td><td>{ndone}/{len(cc)}</td><td>{html.escape(cc[0]["parameter"])}</td></tr>')
    body.append('</table>')
    if qc:
        body.append('<h2>质量检查警告</h2><table><tr><th>Case</th><th>检查项</th><th>数值</th></tr>')
        for case_id,check,value in qc:
            display='UNAVAILABLE' if value is None else f'{value:.7g}'
            body.append(f'<tr><td>{html.escape(str(case_id))}</td><td>{html.escape(str(check))}</td><td>{display}</td></tr>')
        body.append('</table>')
    else:body.append('<p>当前没有触发自动质量阈值预警，<b>不等于电磁计算已经收敛或校准完成</b>。</p>')
    body.append('<h2>对照图</h2>')
    if not images:body.append('<p>暂时没有完成的仿真数据，因此没有物理结果图。</p>')
    for image in images:
        body.append('<div><img src="'+html.escape(str(image.relative_to(output)).replace('\\','/'))+'"></div>')
    body.append('<h2>完整任务清单</h2><table><tr><th>Case</th><th>值</th><th>状态</th><th>日志</th></tr>')
    for c in cases:
        rec=report_records[c['id']]
        url=f"{c['id']}/worker.log"
        detail=rec.get('read_error') or rec['status'].get('error','') or ''
        body.append(f'<tr><td>{html.escape(c["id"])}</td><td>{html.escape(str(c["value"]))}</td><td>{html.escape(rec["state"])}<br><small>{html.escape(detail)}</small></td><td><a href="{html.escape(url)}">日志</a></td></tr>')
    body.append(f'</table><p><small>近场在每组、每个偏振内共用颜色上限。相位只在幅值超过max({absolute_floor:g}, {relative_floor:g}×该分量最大幅值)时展示。请结合具体物理边界和收敛检验判断结果。</small></p></html>')
    (output/'report.html').write_text('\n'.join(body),encoding='utf8')
