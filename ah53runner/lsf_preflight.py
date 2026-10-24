"""Separate, solver-free CAD construction checks for complete LSF tasks."""
from __future__ import annotations

from collections import Counter
import datetime
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import tempfile
import uuid
from urllib.parse import quote

import numpy as np

from .lsf_config import inspect_lsf
from .lsf_runtime import plan_lsf

ROOT = Path(__file__).resolve().parents[1]
AH53_SOURCES = {
    '414d3e61ba2c471f64f1c09de8d770213ecde0b1e8a3dbb57318e6ba48514d03',
    'e424dc4f370245c384b742874801691a5bd19b214e433ad3488acc329481d3dc',
}
PROPERTIES = (
    'x', 'y', 'z', 'x min', 'x max', 'y min', 'y max', 'z min', 'z max',
    'x span', 'y span', 'z span', 'material', 'index', 'enabled',
    'rotation 1', 'rotation 2', 'rotation 3', 'first axis', 'second axis', 'third axis',
    'vertices', 'radius', 'radius 2', 'inner radius', 'outer radius',
    'mesh order', 'override mesh order from material database', 'dimension',
    'injection axis', 'direction', 'monitor type', 'wavelength start', 'wavelength stop',
    'dx', 'dy', 'dz', 'x min bc', 'x max bc', 'y min bc', 'y max bc',
    'z min bc', 'z max bc', 'polarization angle', 'angle theta',
)


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf8')


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint():
    result = hashlib.sha256()
    for name in ('ah53runner/lsf_preflight.py', 'ah53runner/lsf_build_guard.py',
                 'ah53runner/lsf_geometry_checks.py', 'run_lsf_preflight_worker.py',
                 'ah53runner/lsf_config.py', 'ah53runner/lsf_worker.py', 'ah53runner/lsf_runtime.py'):
        result.update((ROOT / name).read_bytes())
    return result.hexdigest()[:16]


def native_value(value):
    if isinstance(value, dict):
        return {str(k): native_value(v) for k, v in value.items()}
    if isinstance(value, (str, bool)) or value is None:
        return value
    array = np.asarray(value)
    if array.size == 1:
        value = array.reshape(-1)[0].item()
        if isinstance(value, complex):
            return {'real': value.real, 'imag': value.imag}
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    return [native_value(v) for v in array.tolist()]


def expected_parameters(source, changes):
    parameters = {}
    for item in inspect_lsf(source)['parameters']:
        if not item.get('exposed'):
            continue
        name, index = item['variable'], item.get('index_1based')
        if index is None:
            parameters[name] = item['value']
        else:
            values = parameters.setdefault(name, [])
            while len(values) < index:
                values.append(None)
            values[index - 1] = item['value']
    for change in changes:
        label = change['label']
        if '(' in label:
            name, tail = label.split('(', 1)
            parameters[name][int(tail.rstrip(')')) - 1] = change['value']
        else:
            parameters[label] = change['value']
    return parameters


def capture_snapshot(session, known_ah53=False):
    """Read actual CAD properties; unknown groups/shapes stay explicitly unknown."""
    objects, unverified = [], []
    def walk(scope, depth=0):
        if depth > 20:
            unverified.append(f'Group nesting beyond 20 levels: {scope}')
            return
        session.groupscope(scope)
        session.selectall()
        count = int(session.getnumber())
        if not count:
            return
        # One native ID query avoids a Python/API round trip per object.
        identifiers = str(session.getid()).splitlines()
        names = [identifier.rsplit('::', 1)[-1].split('#', 1)[0] for identifier in identifiers]
        counts, occurrence = Counter(names), Counter()
        for name in names:
            occurrence[name] += 1
            path = scope + '::' + name
            index = occurrence[name]
            if counts[name] > 1:
                unverified.append(f'Duplicate name; object index used: {path}#{index}')
            available = set(str(session.getnamed(path)).splitlines())
            wanted = [prop for prop in PROPERTIES if prop in available]
            if counts[name] == 1 and wanted:
                props = native_value(session.getnamed(path, wanted))
            else:
                props = {prop: native_value(session.getnamed(path, prop, index)) for prop in wanted}
            if 'dimension' in available:
                kind = 'FDTD'
            elif 'injection axis' in available:
                kind = 'Source'
            elif 'monitor type' in available:
                kind = 'Monitor'
            elif 'vertices' in available:
                kind = 'Polygon'
            elif any(prop in available for prop in ('radius', 'inner radius', 'outer radius')):
                kind = 'CurvedGeometry'
            elif 'material' in available:
                kind = 'Rectangle' if all(p in available for p in ('x min', 'x max', 'y min', 'y max', 'z min', 'z max')) else 'Geometry'
            elif 'dx' in available:
                kind = 'Mesh'
            else:
                kind = 'UnknownOrGroup'
            authenticated_type = known_ah53 and scope == '::model' and (
                name in {'substrate', 'ridge_1', 'ridge_2', 'ridge_3', 'FDTD', 'mesh_all', 'source', 'R', 'INC1', 'INC2'}
                or name.startswith('film_seg_'))
            objects.append({'name': name, 'path': path + (f'#{index}' if counts[name] > 1 else ''),
                            'type': kind, 'type_inferred': not authenticated_type,
                            'type_provenance': 'authenticated AH53 source plus native properties' if authenticated_type else 'native property inference',
                            'properties': props})
            if kind in ('Polygon', 'CurvedGeometry', 'Geometry'):
                unverified.append(f'{path}: exact curved/polygon overlap is not certified by bounding boxes.')
            if kind == 'UnknownOrGroup':
                unverified.append(f'{path}: group/object type not fully classified.')
                if counts[name] == 1:
                    try:
                        walk(path, depth + 1)
                    except Exception as exc:
                        unverified.append(f'{path}: cannot enumerate children: {exc}')
        session.groupscope(scope)
    walk('::model')
    session.groupscope('::model')
    return {'objects': objects, 'unverified': unverified,
            'classification': 'Property-based classification; CAD shape type is not assumed for unknown objects.'}


def draw_cad_projection(snapshot, folder):
    """Plot measured CAD bounds; this is labelled as a projection, not a screenshot."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    fig, axis = plt.subplots(figsize=(13, 4.5))
    materials = {}
    known = [obj for obj in snapshot['objects'] if obj['type'] == 'Rectangle']
    for obj in known:
        props = obj['properties']
        if not all(key in props for key in ('x min', 'x max', 'y min', 'y max')):
            continue
        if props.get('enabled', 1) == 0:
            continue
        key = str(props.get('material', '?')) + ' / n=' + str(props.get('index', '?'))
        if key not in materials:
            materials[key] = len(materials)
            label = key
        else:
            label = None
        color = plt.get_cmap('tab10')(materials[key] % 10)
        inferred = obj.get('type_inferred', True)
        axis.add_patch(Rectangle((props['x min'] * 1e6, props['y min'] * 1e6),
            (props['x max'] - props['x min']) * 1e6, (props['y max'] - props['y min']) * 1e6,
            facecolor='none' if inferred else color, edgecolor=color,
            alpha=0.65, linestyle='--' if inferred else '-', linewidth=0.6, label=label))
    for obj in snapshot['objects']:
        props = obj['properties']
        if obj['type'] in {'Source', 'Monitor'} and all(key in props for key in ('x min', 'x max', 'y')):
            axis.plot([props['x min'] * 1e6, props['x max'] * 1e6], [props['y'] * 1e6] * 2,
                      '--', linewidth=0.7, label=obj['name'])
    axis.autoscale_view()
    axis.set(xlabel='x (um)', ylabel='y (um)', title='XY projection from actual CAD properties (not a native screenshot)')
    axis.grid(alpha=0.2)
    if known:
        axis.legend(fontsize=7, loc='upper right')
    fig.tight_layout()
    fig.savefig(folder / 'geometry_XY.png', dpi=150)
    plt.close(fig)
    return 'geometry_XY.png'


def run_preflight_case(case, folder, api_dir='', visible=False):
    from .lsf_build_guard import build_only_script
    from .lsf_geometry_checks import evaluate_snapshot
    from .lsf_worker import _copy_inputs, _load_lumapi, _marker_completed
    folder = Path(folder).resolve()
    if case['solver_fingerprint'] != fingerprint():
        raise ValueError('Preflight code changed after planning.')
    guard = build_only_script(case['script'])
    if guard['script'] != case['build_script']:
        raise ValueError('Frozen build-only script differs from the guarded prefix.')
    dependencies = {item['relative_path'].casefold() for item in case['execution'].get('input_files', [])}
    missing = [name for name in guard.get('required_relative_inputs', []) if name.casefold() not in dependencies]
    if missing:
        raise ValueError('请在高级设置中添加建模依赖文件：' + ', '.join(missing))
    _copy_inputs(folder, case['execution'])
    (folder / 'build_only.lsf').write_bytes(guard['script'].encode('utf8'))
    dump(folder / 'build_boundary.json', {k: v for k, v in guard.items() if k != 'script'})
    lumapi = _load_lumapi(api_dir or case['execution']['api_dir'])
    checks = []
    images = []
    with lumapi.FDTD(hide=not visible) as session:
        session.cd(folder.as_posix())
        version = str(session.version())
        token = uuid.uuid4().hex
        source, error, completed = ('AH53_preflight_' + label + token for label in ('source', 'error', 'completed'))
        session.putv(source, guard['script'] + '\n' + completed + '=1;\n')
        session.eval('try { eval(' + source + '); } catch(' + error + ');')
        try:
            native_error = session.getv(error)
        except Exception:
            native_error = ''
        if native_error is not None and str(native_error).strip():
            raise RuntimeError('Native build-only LSF error: ' + str(native_error))
        try:
            finished = _marker_completed(session.getv(completed))
        except Exception:
            finished = False
        if not finished:
            raise RuntimeError('Build-only LSF ended early (break/exit); geometry is incomplete.')
        if int(session.layoutmode()) != 1:
            raise RuntimeError('Expected CAD layout mode; refusing to label this a build-only result.')
        checks.append({'code': 'native_build_completed', 'status': 'PASS',
                       'message': 'Lumerical 原生建模段执行完成，仍处于 layout 模式；求解段未执行。'})
        actual_inputs = {}
        for change in case['parameter_changes']:
            label = change['label']
            name = label.split('(', 1)[0]
            value = np.asarray(session.getv(name))
            if '(' in label:
                value = value.reshape(-1, order='F')[int(label.split('(')[1].rstrip(')')) - 1]
            actual = float(np.asarray(value).reshape(-1)[0])
            actual_inputs[label] = actual
            ok = math.isclose(actual, change['value'], rel_tol=1e-12, abs_tol=1e-15)
            checks.append({'code': 'parameter_value', 'status': 'PASS' if ok else 'FAIL',
                           'message': f'{label}: 预期 {change["value"]}，实际 {actual}',
                           'details': {'parameter': label, 'expected': change['value'], 'actual': actual}})
        snapshot = capture_snapshot(session, case['geometry_profile'] == 'ah53_xy')
        session.save((folder / 'model.fsp').as_posix())
        dimensions = [obj['properties'].get('dimension') for obj in snapshot['objects'] if obj['type'] == 'FDTD']
        views = ('XY',) if dimensions == ['2D'] else ('XY', 'XZ', 'YZ')
        # Hidden CAD exportview failed on this host even for an ASCII path.
        # The fast default uses a labelled plot of measured properties instead.
        with tempfile.TemporaryDirectory(prefix='ah53_cad_') as temporary:
            views = views if visible else ()
            for view in views:
                target = Path(temporary) / ('cad_' + view)
                try:
                    session.exportview(target.as_posix(), view)
                    image = target.with_suffix('.png')
                    if not image.is_file() or image.stat().st_size <= 0:
                        raise ValueError('No nonempty native image was generated.')
                    shutil.copy2(image, folder / image.name)
                    images.append(image.name)
                except Exception as exc:
                    checks.append({'code': 'native_view', 'status': 'WARN',
                                   'message': f'{view} 原生截图未成功：{exc}'})
    checks.extend(evaluate_snapshot(snapshot, case['expected_parameters'], case['geometry_profile']))
    if case['geometry_profile'] == 'ah53_xy':
        fdtds = [obj for obj in snapshot['objects'] if obj['name'] == 'FDTD']
        correct = len(fdtds) == 1 and fdtds[0]['properties'].get('dimension') == '2D'
        checks.append({'code': 'ah53_dimension', 'status': 'PASS' if correct else 'FAIL',
                       'message': '已知 AH53 XY 脚本要求实际 FDTD 维度为 2D。'})
    try:
        images.append(draw_cad_projection(snapshot, folder))
    except Exception as exc:
        checks.append({'code': 'geometry_projection', 'status': 'WARN',
                       'message': f'CAD 属性投影未生成，请打开 model.fsp 查看：{exc}'})
    dump(folder / 'geometry_snapshot.json', snapshot)
    dump(folder / 'checks.json', {'checks': checks, 'actual_inputs': actual_inputs})
    for entry in case['execution'].get('input_files', []):
        if file_hash(folder / entry['relative_path']) != entry['sha256']:
            raise ValueError('A native script changed a staged dependency: ' + entry['relative_path'])
    result = {'schema_version': 'lsf-build-preflight-1.0', 'execution_kind': 'lsf_preflight',
              'digest': case['digest'], 'solver_fingerprint': case['solver_fingerprint'],
              'source_sha256': case['source_sha256'], 'script_sha256': case['script_sha256'],
              'build_sha256': hashlib.sha256(guard['script'].encode()).hexdigest(),
              'build_completed': True, 'fdtd_solver_executed': False, 'layout_mode': True,
              'lumerical_version': version, 'api_dir': api_dir or case['execution']['api_dir'],
              'geometry_profile': case['geometry_profile'], 'checks': checks, 'images': images,
              'visualization': 'geometry_XY.png is a plot of measured CAD object bounds, not a native screenshot; curved/rotated/unknown shapes are not certified by this projection.',
              'geometry_passed': not any(c['status'] == 'FAIL' for c in checks),
              'files': [{'path': name, 'sha256': file_hash(folder / name), 'size_bytes': (folder / name).stat().st_size}
                        for name in ['model.fsp', 'build_only.lsf', 'build_boundary.json', 'geometry_snapshot.json', 'checks.json', *images]]}
    dump(folder / 'preflight_result.json', result)
    return result


def load_preflight_artifacts(folder, case, require_done=True):
    folder = Path(folder)
    result = json.loads((folder / 'preflight_result.json').read_text(encoding='utf8'))
    for key in ('digest', 'solver_fingerprint', 'source_sha256', 'script_sha256'):
        if result.get(key) != case[key]:
            raise ValueError('Preflight identity mismatch: ' + key)
    if result['solver_fingerprint'] != fingerprint() or not result.get('build_completed') or result.get('fdtd_solver_executed') is not False:
        raise ValueError('Invalid build-only completion record.')
    if require_done:
        status = json.loads((folder / 'status.json').read_text(encoding='utf8'))
        if status.get('status') != 'DONE':
            raise ValueError('Build-only worker is not DONE.')
    if hashlib.sha256(case['script'].encode()).hexdigest() != case['script_sha256']:
        raise ValueError('Frozen full script checksum mismatch.')
    if file_hash(folder / 'script.lsf') != case['script_sha256']:
        raise ValueError('Saved full script changed.')
    if hashlib.sha256(case['build_script'].encode()).hexdigest() != result.get('build_sha256'):
        raise ValueError('Frozen build script checksum mismatch.')
    required = {'model.fsp', 'build_only.lsf', 'build_boundary.json', 'geometry_snapshot.json', 'checks.json'}
    if not required.issubset(record['path'] for record in result['files']):
        raise ValueError('Missing required preflight artifacts.')
    for record in result['files']:
        target = (folder / record['path']).resolve()
        if not target.is_relative_to(folder.resolve()) or not target.is_file() or target.stat().st_size != record['size_bytes'] or file_hash(target) != record['sha256']:
            raise ValueError('Preflight artifact changed: ' + record['path'])
    evidence = json.loads((folder / 'checks.json').read_text(encoding='utf8'))
    if evidence['checks'] != result['checks']:
        raise ValueError('Preflight check records disagree.')
    return result


def write_report(output, cases):
    from .lsf_geometry_checks import compare_snapshots
    rows, previous = [], None
    for case in cases:
        folder = output / case['id']
        state_path = folder / 'status.json'
        state = json.loads(state_path.read_text(encoding='utf8')) if state_path.exists() else {'status': 'PENDING'}
        row = {'id': case['id'], 'parameter': case['parameter'], 'value': case['value'],
               'state': state.get('status'), 'error': state.get('error'), 'checks': [], 'changes': []}
        if state.get('status') == 'DONE':
            try:
                result = load_preflight_artifacts(folder, case)
                row.update(checks=result['checks'], images=result['images'], profile=result['geometry_profile'],
                           version=result['lumerical_version'])
                snapshot = json.loads((folder / 'geometry_snapshot.json').read_text(encoding='utf8'))
                if previous is not None:
                    changes = compare_snapshots(previous[1], snapshot)
                    row['changes'] = changes
                    if not changes and case['value'] != previous[0]['value']:
                        row['checks'].append({'code': 'unchanged_scan_geometry', 'status': 'WARN',
                                              'message': '扫描值改变，但读取的几何/材料/设置未改变；请确认参数是否真正用于建模。'})
                previous = (case, snapshot)
                row['state'] = 'FAIL' if any(c['status'] == 'FAIL' for c in row['checks']) else (
                    'REVIEW' if any(c['status'] in {'WARN', 'UNVERIFIED'} for c in row['checks']) else 'PASS')
            except Exception as exc:
                row.update(state='INVALID', error=str(exc))
        rows.append(row)
    dump(output / 'summary.json', {'fdtd_solver_executed': False, 'cases': rows})
    esc = lambda v: html.escape(str(v), quote=True)
    body = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>LSF 建模预检</title>',
            '<style>body{font:15px/1.6 system-ui;margin:24px;color:#223}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px;text-align:left}img{max-width:100%;width:900px;border:1px solid #ddd}code{overflow-wrap:anywhere}.PASS{color:#176332}.FAIL,.FAILED,.INVALID{color:#a21d1d}.WARN,.UNVERIFIED,.REVIEW{color:#835300}pre{white-space:pre-wrap}details{margin:10px 0}</style>',
            '<h1>LSF 建模预检：未执行 FDTD 求解</h1>',
            '<p>检查原生 CAD 建模段、参数实际值、对象尺寸与材料，并比较扫描点。PASS 仅针对报告列出的规则；REVIEW 表示仍有待确认项目。原脚本求解及后处理段未执行，电磁精度和任意设计意图未获认证。</p>',
            '<p>geometry_XY.png 根据实际 CAD 属性绘制矩形投影，不是 Lumerical 原生截图；未知、曲面或旋转几何不能只凭此图认定形状正确。可打开 model.fsp 检查原生模型。</p>',
            '<p><a href="summary.json">完整检查与变化记录 JSON</a></p>']
    for row in rows:
        relative = quote(row['id'], safe='')
        body.append(f'<h2>{esc(row["parameter"])} = {esc(row["value"])} · <span class="{esc(row["state"])}">{esc(row["state"])}</span></h2>')
        body.append('<p>' + ' | '.join(f'<a href="{relative}/{name}">{label}</a>' for name, label in
                     [('script.lsf', '完整扫描 LSF'), ('build_only.lsf', '实际执行建模段'), ('model.fsp', 'CAD 工程'),
                      ('geometry_snapshot.json', '实际对象属性'), ('checks.json', '规则详情'), ('worker.log', '日志')]) + '</p>')
        if row.get('error'):
            body.append('<pre>' + esc(row['error']) + '</pre>')
        body.append('<table><tr><th>状态</th><th>检查项</th></tr>')
        for check in row['checks']:
            body.append(f'<tr><td class="{esc(check["status"])}">{esc(check["status"])}</td><td>{esc(check["message"])}</td></tr>')
        body.append('</table>')
        if row['changes']:
            body.append('<details><summary>与前一扫描点相比，实际改变的对象属性</summary><pre>' + esc(json.dumps(row['changes'], ensure_ascii=False, indent=2)) + '</pre></details>')
        for name in row.get('images', []):
            body.append(f'<a href="{relative}/{quote(name)}"><img alt="{esc(name)}：CAD 几何预览" src="{relative}/{quote(name)}"></a>')
    body.append('</html>')
    (output / 'report.html').write_text('\n'.join(body), encoding='utf8')
    return rows


def run_preflight(cfg, path, args, runner):
    from .lsf_build_guard import build_only_script
    planned = plan_lsf(cfg, path)
    if args.only:
        planned = [case for case in planned if case['group'] == args.only]
        if not planned:
            raise ValueError('No LSF cases for ' + args.only)
    if args.limit:
        planned = planned[:args.limit]
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    output = Path(cfg['output_dir']) / 'preflight' / stamp
    output.mkdir(parents=True)
    dump(output / 'task_snapshot.json', cfg)
    cases = []
    print('PRECHECK: native CAD construction only; NO FDTD SOLVER RUNS.', flush=True)
    print('PRECHECK OUTPUT:', output, flush=True)
    source = cfg['source']['script']
    profile = 'ah53_xy' if cfg['source']['sha256'] in AH53_SOURCES else 'generic'
    with runner.QueueLock(output):
        for original in planned:
            case = dict(original)
            case.update(execution_kind='lsf_preflight', solver_fingerprint=fingerprint(), geometry_profile=profile)
            case['expected_parameters'] = expected_parameters(source, case['parameter_changes'])
            case['expected_parameters']['__selected_parameter__'] = case['parameter']
            folder = output / case['id']
            folder.mkdir()
            try:
                guard = build_only_script(case['script'])
                case['build_script'] = guard['script']
                case['digest'] = hashlib.sha256((original['digest'] + case['solver_fingerprint'] + guard['script']).encode()).hexdigest()
                (folder / 'script.lsf').write_bytes(case['script'].encode('utf8'))
                dump(folder / 'parameters.json', {'changes': case['parameter_changes']})
                dump(folder / 'case.json', case)
            except ValueError as exc:
                dump(folder / 'status.json', {'status': 'FAILED', 'error': str(exc)})
                print('PRECHECK REJECTED:', case['id'], str(exc), flush=True)
            cases.append(case)
        dump(output / 'manifest.json', {'mode': 'build_only', 'cases': [{'id': c['id'], 'value': c['value']} for c in cases]})
        interrupted = False
        for case in cases:
            folder = output / case['id']
            if (folder / 'status.json').exists():
                continue
            if args.stop_file and Path(args.stop_file).exists():
                interrupted = True
                break
            print('PRECHECK START', case['id'], flush=True)
            try:
                success, elapsed = runner.launch_case(output, case, cfg['lumerical_api_python'],
                    min(600, int(cfg.get('max_case_minutes', 90) * 60)), args.visible, stop_file=args.stop_file)
                print('PRECHECK BUILT' if success else 'PRECHECK FAILED', case['id'], f'{elapsed:.1f}s', flush=True)
            except KeyboardInterrupt:
                interrupted = True
                break
        rows = write_report(output, cases)
    print('PRECHECK REPORT:', output / 'report.html', flush=True)
    print('PRECHECK STATES:', dict(Counter(row['state'] for row in rows)), flush=True)
    return 130 if interrupted else (1 if any(r['state'] in {'FAIL', 'FAILED', 'INVALID'} for r in rows) else 0)
