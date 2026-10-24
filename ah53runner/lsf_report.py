"""Escaped, artifact-verified execution reports for complete LSF tasks.

No general optical interpretation is assigned to native LSF results here.
"""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path
from urllib.parse import quote

from .lsf_runtime import load_lsf_artifacts


def _text(value):
    return html.escape(str(value), quote=True)


def _status(path):
    if not path.exists():
        return {'status': 'PENDING'}
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError('status.json root is not an object')
        return value
    except (ValueError, OSError) as exc:
        return {'status': 'INVALID', 'error': f'Cannot read status.json: {exc}'}


def _csv_cell(value):
    # Spreadsheet readers must not execute a user-controlled parameter label.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def _link(output, path, label=None):
    try:
        resolved = path.resolve()
        relative = resolved.relative_to(output.resolve())
        if not resolved.is_file():
            return ''
    except (ValueError, OSError):
        return ''
    return f'<a href="{_text(quote(relative.as_posix(), safe="/"))}">{_text(label or relative.name)}</a>'


def make_lsf_report(output, cases, cfg) -> Path:
    """Write report.html + summary.csv, accepting DONE only after verification.

    ``cases`` are planned complete-LSF cases; this function never starts a solver
    and never changes a case's status or native artifacts.
    """
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    counts = {}
    for case in cases:
        folder = (output / str(case['id'])).resolve()
        if not folder.is_relative_to(output):
            raise ValueError('Case directory escapes report output')
        status = _status(folder / 'status.json')
        state = str(status.get('status', 'PENDING')).upper()
        error = str(status.get('error') or '')
        verified = None
        if state == 'DONE':
            try:
                verified = load_lsf_artifacts(folder, case)
            except Exception as exc:
                state = 'INVALID'
                error = f'DONE artifact validation failed: {exc}'
        counts[state] = counts.get(state, 0) + 1
        links = []
        linked = set()
        for filename in ('script.lsf', 'parameters.json', 'case.json', 'status.json', 'worker.log'):
            link = _link(output, folder / filename, filename)
            if link:
                links.append(link)
                linked.add(filename)
        version = ''
        warnings = []
        if verified is not None:
            result = verified.get('script_result', {})
            version = result.get('lumerical_version', '')
            warnings = result.get('warnings') or []
            if not isinstance(warnings, list):
                warnings = [str(warnings)]
            for record in verified.get('files', []):
                relative = record.get('path') if isinstance(record, dict) else None
                if not isinstance(relative, str) or relative in linked:
                    continue
                link = _link(output, folder / relative, relative)
                if link:
                    links.append(link)
                    linked.add(relative)
            for filename in ('metadata.json', 'script_result.json'):
                if filename not in linked:
                    link = _link(output, folder / filename, filename)
                    if link:
                        links.append(link)
        else:
            # Failed attempts remain inspectable, without presenting partial
            # model files as verified completed results.
            link = _link(output, folder / 'model.fsp', 'model.fsp（未验证/部分文件）')
            if link:
                links.append(link)
        rows.append({'case_id': case['id'], 'group': case.get('group', ''),
                     'parameter': case.get('parameter', ''), 'value': case.get('value', ''),
                     'state': state, 'elapsed_s': status.get('elapsed_s', ''),
                     'error': error, 'version': version, 'warnings': warnings,
                     'links': links, 'digest': case.get('digest', ''),
                     'mode': case.get('execution', {}).get('mode', 'script')})
    columns = ['case_id', 'group', 'parameter', 'value', 'state', 'elapsed_s',
               'mode', 'version', 'digest', 'error']
    with (output / 'summary.csv').open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_csv_cell(row.get(key, '')) for key in columns])
    body = [
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>完整 LSF 扫描执行报告</title>',
        '<style>body{font:15px/1.6 system-ui,sans-serif;margin:28px;color:#222}',
        'table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccd2d8;',
        'padding:8px;vertical-align:top;text-align:left}th{background:#edf2f6}',
        'code{overflow-wrap:anywhere}a{color:#165ca5}li{margin:3px 0}',
        '.notice{background:#f2f5f8;padding:14px;border-left:4px solid #547a9c}',
        '.DONE{color:#1c7033}.FAILED,.INVALID{color:#a51f25}.error{white-space:pre-wrap;overflow-wrap:anywhere}',
        '</style><body><h1>完整 LSF 扫描执行报告</h1>',
        f'<p>任务：{_text(cfg.get("name", "LSF task"))} · 原脚本：{_text(cfg.get("source", {}).get("filename", ""))}</p>',
        '<div class="notice"><strong>执行和物理分析分开记录。</strong> ',
        'DONE 表示原 LSF 执行完成，任务指纹、模型及记录的输出文件通过完整性校验；',
        '它不表示物理精度、能量闭合或光学结果通过验收。执行模式为 <code>script</code>：',
        'LSF 自己决定是否调用 run；如果原脚本只建模，DONE 也可能没有求解结果。',
        '调度器不追加 run，不重建原几何，不将未知三维结果套入二维 Jones 后处理。</div>',
        '<p>' + ' · '.join(f'{_text(key)}: {value}' for key, value in sorted(counts.items())) + '</p>',
        '<p><a href="summary.csv">下载执行汇总 CSV</a></p>',
        '<table><thead><tr><th>案例 / 参数</th><th>状态 / 耗时</th><th>记录与原生文件</th><th>错误 / 提示</th></tr></thead><tbody>',
    ]
    for row in rows:
        details = [row['error']] if row['error'] else []
        details.extend(str(item) for item in row['warnings'])
        files = '<ul>' + ''.join(f'<li>{link}</li>' for link in row['links']) + '</ul>'
        state_class = row['state'] if row['state'] in {'DONE', 'FAILED', 'INVALID'} else ''
        body.append('<tr><td><strong>' + _text(row['case_id']) + '</strong><br>' +
                    _text(row['group']) + '<br><code>' + _text(row['parameter']) + ' = ' +
                    _text(row['value']) + '</code></td><td><strong class="' + state_class + '">' +
                    _text(row['state']) + '</strong><br>' + _text(row['elapsed_s']) +
                    ' s<br>mode: ' + _text(row['mode']) + '<br>' + _text(row['version']) +
                    '</td><td>' + files + '</td><td class="error">' +
                    _text('\n'.join(details)) + '</td></tr>')
    body.append('</tbody></table></body></html>')
    target = output / 'report.html'
    target.write_text('\n'.join(body), encoding='utf-8')
    return target
