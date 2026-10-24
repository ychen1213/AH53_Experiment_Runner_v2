"""Local complete-LSF task editor and sweep queue launcher."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ah53runner.lsf_config import (
    expand_bundle,
    inspect_lsf,
    make_bundle,
    parse_values,
    range_values,
)


def read_lsf(path):
    """Decode without replacement so the embedded source remains intact."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode('utf-8-sig'), 'UTF-8'
    except UnicodeDecodeError:
        try:
            return raw.decode('gb18030'), 'GB18030'
        except UnicodeDecodeError as exc:
            raise ValueError('无法读取脚本编码，请另存为 UTF-8 后重试。') from exc


def single_value(text):
    values = parse_values(text)
    if len(values) != 1:
        raise ValueError('此处必须填写一个数值。')
    return values[0]


def parameter_by_label(inspection, label):
    matches = [p for p in inspection['parameters'] if p['label'] == label]
    if not matches:
        raise ValueError(f'没有找到参数 {label!r}，请先运行 inspect 查看精确标签。')
    if len(matches) != 1:
        raise ValueError(f'参数标签 {label!r} 不唯一，不能自动选择。请使用界面核对行号。')
    parameter = matches[0]
    if not parameter['editable']:
        raise ValueError(f'参数 {label!r} 不可编辑：{parameter.get("reason", "不支持的赋值")}')
    if not parameter.get('exposed'):
        raise ValueError(f'参数 {label!r} 不在安全输入区：{parameter.get("exposure_reason", "请提取到输入区")}')
    return parameter


def write_json_new(path, bundle):
    destination = Path(path).expanduser()
    if destination.suffix.lower() != '.json':
        raise ValueError('导出文件必须使用 .json 扩展名。')
    encoded = json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(encoded)
    return destination


def values_from_args(args):
    if args.values is not None:
        if args.stop is not None or args.step is not None or args.count is not None:
            raise ValueError('--values 不能与 --stop、--step 或 --count 一起使用。')
        return parse_values(args.values)
    if args.stop is None:
        raise ValueError('使用 --start 时必须同时填写 --stop。')
    if args.step is None and args.count is None:
        raise ValueError('范围扫描必须选择 --step 或 --count。')
    return range_values(single_value(args.start), single_value(args.stop),
                        step=single_value(args.step) if args.step is not None else None,
                        count=args.count)


def run_cli(args):
    if args.command == 'inspect':
        script, encoding = read_lsf(args.lsf)
        result = inspect_lsf(script)
        result['source_filename'] = Path(args.lsf).name
        result['source_encoding'] = encoding
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if args.command == 'export':
        script, encoding = read_lsf(args.lsf)
        inspection = inspect_lsf(script)
        selected = parameter_by_label(inspection, args.parameter)
        fixed = {}
        for entry in args.fixed:
            if '=' not in entry:
                raise ValueError('--set 格式应为 参数标签=数值，例如 film_count=1。')
            label, raw = entry.split('=', 1)
            parameter = parameter_by_label(inspection, label.strip())
            if parameter['id'] in fixed:
                raise ValueError(f'重复设置固定参数：{label.strip()}')
            fixed[parameter['id']] = single_value(raw)
        bundle = make_bundle(script, Path(args.lsf).name, selected['id'],
                             values_from_args(args), fixed_values=fixed, name=args.name)
        path = write_json_new(args.output, bundle)
        print(f'已导出 {path}（输入编码 {encoding}；仅保存 JSON，未执行 LSF）。')
        for warning in bundle.get('inspection', {}).get('warnings', []):
            print(f'原始脚本提示（固定覆盖前）：{warning}', file=sys.stderr)
        return
    if args.command == 'expand':
        bundle = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        cases = expand_bundle(bundle)
        directory = Path(args.output_dir).expanduser()
        paths = [directory / f'case_{index:03d}.lsf' for index in range(len(cases))]
        existing = [str(path) for path in paths if path.exists()]
        if existing:
            raise ValueError(f'拒绝覆盖已有文件：{existing[0]}。请选择新的输出目录。')
        directory.mkdir(parents=True, exist_ok=True)
        for path, case in zip(paths, cases):
            with path.open('x', encoding='utf-8', newline='') as stream:
                stream.write(case['script'])
        print(f'已展开 {len(cases)} 个脚本到 {directory}，从 case_000.lsf 开始；未执行脚本。')
        return
    raise ValueError(f'未知命令：{args.command}')


def launch_gui(initial_lsf=None):
    # Import only for the graphical mode; command-line use does not require Tk.
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    class Editor:
        def __init__(self, window):
            self.window = window
            self.filename = 'pasted_script.lsf'
            self.source_dir = None
            self.snapshot = None
            self.inspection = None
            self.selected_id = None
            self.fixed = {}
            self.rows = {}
            self.bundle = None
            self.loading = False
            self.process = None
            self.active_command = None
            self.events = queue.Queue()
            self.stop_path = None
            self.closing = False
            self.disabled_widgets = []
            self.dependencies = []
            self.status = tk.StringVar(value='导入或粘贴 LSF，然后点击“解析参数”。')
            self.source_label = tk.StringVar(value='来源：粘贴的脚本')
            self.target = tk.StringVar(value='尚未选择')
            self.fixed_input = tk.StringVar()
            self.scan_mode = tk.StringVar(value='values')
            self.values_input = tk.StringVar(value='0, 100, 200')
            self.start = tk.StringVar(value='0')
            self.stop = tk.StringVar(value='400')
            self.spacing_mode = tk.StringVar(value='step')
            self.spacing = tk.StringVar(value='100')
            self.name = tk.StringVar(value='lsf_scan')
            self.output_dir = tk.StringVar(value=str(ROOT / 'output_lsf_pasted_script'))
            installed = Path('D:/Ansys Inc/v252/Lumerical/api/python')
            self.api_dir = tk.StringVar(value=str(installed) if installed.is_dir() else '')
            self.timeout = tk.StringVar(value='90')
            self.required_outputs = tk.StringVar(value='')
            self.dependency_label = tk.StringVar(value='未添加依赖文件')
            self.show_advanced = tk.BooleanVar(value=False)
            self.show_json = tk.BooleanVar(value=False)
            self.build_ui(ttk, scrolledtext)
            for variable in (self.scan_mode, self.values_input, self.start, self.stop,
                             self.spacing_mode, self.spacing, self.name, self.output_dir,
                             self.api_dir, self.timeout, self.required_outputs):
                variable.trace_add('write', lambda *_: self.invalidate())
            self.source.bind('<<Modified>>', self.source_changed)
            self.window.protocol('WM_DELETE_WINDOW', self.close)
            self.window.after(150, self.poll_process)

        def build_ui(self, ttk, scrolledtext):
            self.window.title('完整 LSF 参数扫描与求解队列')
            self.window.geometry('1380x900')
            self.window.minsize(1040, 720)
            outer = ttk.Frame(self.window, padding=10)
            outer.pack(fill='both', expand=True)
            ttk.Label(outer, text='导入完整 LSF → 选择输入参数 → 规划保存 → 启动队列',
                      font=('', 11, 'bold')).pack(anchor='w')
            ttk.Label(outer, text='原 LSF 决定二维/三维结构、材料、光源、监视器、run 与后处理；本工具只注入选定输入并逐案例执行，不额外调用 run。',
                      wraplength=1300).pack(anchor='w', pady=(4, 8))
            split = ttk.Panedwindow(outer, orient='horizontal')
            split.pack(fill='both', expand=True)
            left, right = ttk.Frame(split), ttk.Frame(split)
            self.edit_panels = [left]
            split.add(left, weight=3)
            split.add(right, weight=2)
            source_bar = ttk.Frame(left)
            source_bar.pack(fill='x')
            ttk.Button(source_bar, text='导入 LSF 文件', command=self.open_file).pack(side='left')
            ttk.Button(source_bar, text='解析参数', command=self.parse).pack(side='left', padx=6)
            ttk.Label(source_bar, textvariable=self.source_label).pack(side='left', padx=4)
            self.source = scrolledtext.ScrolledText(left, wrap='none', height=12, undo=True,
                                                   font=('Consolas', 10))
            self.source.pack(fill='both', expand=True, pady=6)
            ttk.Label(left, text='仅输入区可暴露数值可选择；派生量和后处理常量锁定。数组下标从 1 开始。',
                      wraplength=740).pack(anchor='w')
            table_frame = ttk.Frame(left)
            table_frame.pack(fill='both', expand=True, pady=5)
            self.table = ttk.Treeview(table_frame, columns=('line', 'value', 'fixed', 'status'),
                                      height=9, selectmode='browse')
            for column, title, width in (('#0', '参数', 180), ('line', '行号', 48),
                                          ('value', '原值', 100), ('fixed', '固定值', 85),
                                          ('status', '状态 / 原因', 210)):
                self.table.heading(column, text=title)
                self.table.column(column, width=width, minwidth=40, stretch=column in ('#0', 'status'))
            scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
            self.table.configure(yscrollcommand=scrollbar.set)
            self.table.pack(side='left', fill='both', expand=True)
            scrollbar.pack(side='right', fill='y')
            self.table.tag_configure('locked', foreground='#777777')
            actions = ttk.Frame(left)
            actions.pack(fill='x', pady=(0, 5))
            ttk.Button(actions, text='设为扫描参数', command=self.choose_target).pack(side='left')
            ttk.Entry(actions, textvariable=self.fixed_input, width=13).pack(side='left', padx=(12, 4))
            ttk.Button(actions, text='设置固定值', command=self.set_fixed).pack(side='left')
            ttk.Button(actions, text='取消固定值', command=self.clear_fixed).pack(side='left', padx=4)
            ttk.Label(left, text='数值沿用原 LSF 单位，不自动换算。例如 nm 变量可填 100；以米为单位的变量应填 100e-9。',
                      wraplength=740).pack(anchor='w', pady=(0, 4))
            scan = ttk.LabelFrame(left, text='一个扫描参数，其余参数可保持原值或设置固定值', padding=8)
            scan.pack(fill='x')
            ttk.Label(scan, text='扫描目标：').grid(row=0, column=0, sticky='w')
            ttk.Label(scan, textvariable=self.target).grid(row=0, column=1, columnspan=4, sticky='w')
            ttk.Radiobutton(scan, text='显式数值', value='values', variable=self.scan_mode).grid(row=1, column=0, sticky='w')
            ttk.Entry(scan, textvariable=self.values_input).grid(row=1, column=1, columnspan=4, sticky='ew', pady=3)
            ttk.Radiobutton(scan, text='范围', value='range', variable=self.scan_mode).grid(row=2, column=0, sticky='w')
            ttk.Label(scan, text='起点').grid(row=2, column=1, sticky='w')
            ttk.Entry(scan, textvariable=self.start, width=13).grid(row=2, column=2, sticky='ew')
            ttk.Label(scan, text='终点').grid(row=2, column=3, sticky='w', padx=(8, 0))
            ttk.Entry(scan, textvariable=self.stop, width=13).grid(row=2, column=4, sticky='ew')
            ttk.Combobox(scan, textvariable=self.spacing_mode, values=('step', 'count'),
                         state='readonly', width=9).grid(row=3, column=1, sticky='w', pady=3)
            ttk.Entry(scan, textvariable=self.spacing, width=13).grid(row=3, column=2, sticky='ew')
            ttk.Label(scan, text='step=步长；count=点数').grid(row=3, column=3, columnspan=2, sticky='w', padx=(8, 0))
            ttk.Label(scan, text='任务名称').grid(row=4, column=0, sticky='w')
            ttk.Entry(scan, textvariable=self.name).grid(row=4, column=1, columnspan=4, sticky='ew', pady=3)
            scan.columnconfigure(2, weight=1)
            scan.columnconfigure(4, weight=1)
            ttk.Label(left, text='解析提示 / 限制').pack(anchor='w', pady=(7, 0))
            self.warnings = scrolledtext.ScrolledText(left, height=4, wrap='word', state='disabled')
            self.warnings.pack(fill='x', pady=(3, 0))
            self.json_frame = ttk.Frame(right)
            preview_bar = ttk.Frame(self.json_frame)
            self.edit_panels.append(preview_bar)
            preview_bar.pack(fill='x')
            ttk.Button(preview_bar, text='生成 JSON 预览', command=self.generate).pack(side='left', padx=(8, 4))
            ttk.Button(preview_bar, text='复制 JSON', command=self.copy).pack(side='left', padx=4)
            ttk.Button(preview_bar, text='另存 JSON', command=self.save).pack(side='left', padx=4)
            self.preview = scrolledtext.ScrolledText(self.json_frame, wrap='none', state='disabled',
                                                    height=9, font=('Consolas', 10))
            self.preview.pack(fill='both', expand=True, padx=(8, 0), pady=6)
            ttk.Label(self.json_frame, text='JSON 是自动保存的任务记录。日常使用无需手写；也可查看、复制或另存。',
                      wraplength=460).pack(anchor='w', padx=8)
            runtime = ttk.LabelFrame(right, text='任务输出与执行', padding=8)
            runtime.pack(fill='x', padx=(8, 0), pady=6)
            self.edit_panels.append(runtime)
            ttk.Label(runtime, text='输出目录').grid(row=0, column=0, sticky='w')
            ttk.Entry(runtime, textvariable=self.output_dir).grid(row=1, column=0, sticky='ew')
            ttk.Button(runtime, text='选择…', command=self.choose_output).grid(row=1, column=1, padx=4)
            ttk.Checkbutton(runtime, text='高级设置：API、超时、依赖、预期输出',
                            variable=self.show_advanced, command=self.toggle_advanced).grid(row=2, column=0, columnspan=2, sticky='w', pady=4)
            self.advanced = ttk.Frame(runtime)
            self.advanced.grid(row=3, column=0, columnspan=2, sticky='ew')
            ttk.Label(self.advanced, text='lumapi.py 所在目录').grid(row=0, column=0, sticky='w')
            ttk.Entry(self.advanced, textvariable=self.api_dir).grid(row=1, column=0, columnspan=2, sticky='ew')
            ttk.Label(self.advanced, text='每案例超时（分钟）').grid(row=2, column=0, sticky='w')
            ttk.Entry(self.advanced, textvariable=self.timeout, width=9).grid(row=2, column=1, sticky='w')
            ttk.Label(self.advanced, text='必须产出文件，相对案例目录，用逗号分隔').grid(row=3, column=0, columnspan=2, sticky='w')
            ttk.Entry(self.advanced, textvariable=self.required_outputs).grid(row=4, column=0, columnspan=2, sticky='ew')
            ttk.Button(self.advanced, text='添加依赖文件…', command=self.add_dependencies).grid(row=5, column=0, sticky='w', pady=3)
            ttk.Button(self.advanced, text='清空依赖', command=self.clear_dependencies).grid(row=5, column=1, sticky='w')
            ttk.Label(self.advanced, textvariable=self.dependency_label, wraplength=430).grid(row=6, column=0, columnspan=2, sticky='w')
            self.advanced.columnconfigure(0, weight=1)
            runtime.columnconfigure(0, weight=1)
            self.advanced.grid_remove()
            task_bar = ttk.Frame(right)
            task_bar.pack(fill='x', padx=8, pady=3)
            self.task_buttons = []
            for title, command, row, column in [
                    ('规划并保存', lambda: self.launch_task('plan'), 0, 0),
                    ('仅建模检查（不求解）', lambda: self.launch_task('preflight'), 0, 1),
                    ('启动 / 续跑队列', lambda: self.launch_task('run'), 1, 0),
                    ('重试失败', lambda: self.launch_task('run', retry=True), 1, 1)]:
                button = ttk.Button(task_bar, text=title, command=command)
                button.grid(row=row, column=column, sticky='ew', padx=(0, 4), pady=3)
                self.task_buttons.append(button)
            self.stop_button = ttk.Button(task_bar, text='停止', command=self.request_stop, state='disabled')
            self.stop_button.grid(row=1, column=2, sticky='ew', pady=3)
            task_bar.columnconfigure(0, weight=1)
            task_bar.columnconfigure(1, weight=1)
            ttk.Button(right, text='打开输出目录', command=self.open_output).pack(anchor='w', padx=8, pady=3)
            ttk.Label(right, text='队列日志（停止仅影响本窗口启动的任务）').pack(anchor='w', padx=8)
            self.log = scrolledtext.ScrolledText(right, wrap='word', height=10, state='disabled', font=('Consolas', 9))
            self.log.pack(fill='both', expand=True, padx=(8, 0), pady=4)
            ttk.Checkbutton(right, text='查看任务 JSON（可选）', variable=self.show_json,
                            command=self.toggle_json).pack(anchor='w', padx=8, pady=4)
            ttk.Label(outer, textvariable=self.status, wraplength=1300).pack(anchor='w', pady=(8, 0))

        def show_text(self, widget, value):
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('1.0', value)
            widget.configure(state='disabled')

        def active(self):
            return self.process is not None

        def ensure_idle(self):
            if self.active():
                raise ValueError('任务正在运行，请先停止并等待队列退出后再修改或保存任务。')

        def choose_output(self):
            path = filedialog.askdirectory(title='选择任务输出目录', initialdir=str(ROOT))
            if path:
                self.output_dir.set(path)

        def toggle_advanced(self):
            if self.show_advanced.get():
                self.advanced.grid()
            else:
                self.advanced.grid_remove()

        def toggle_json(self):
            if self.show_json.get():
                self.json_frame.pack(fill='both', expand=True, padx=(8, 0), pady=4)
            else:
                self.json_frame.pack_forget()

        def add_dependencies(self):
            if self.source_dir is None:
                messagebox.showerror('需要来源目录', '请先从文件导入 LSF。依赖文件必须位于原 LSF 目录或其子目录。')
                return
            paths = filedialog.askopenfilenames(title='选择原 LSF 使用的依赖文件', initialdir=str(self.source_dir))
            try:
                additions = []
                for value in paths:
                    source = Path(value).resolve()
                    try:
                        relative = source.relative_to(self.source_dir).as_posix()
                    except ValueError as exc:
                        raise ValueError('依赖必须位于原 LSF 所在目录内；请先将其复制到脚本目录并调整 LSF 相对路径。') from exc
                    additions.append({'source': str(source), 'relative_path': relative})
                known = {item['relative_path'] for item in self.dependencies}
                self.dependencies.extend(item for item in additions if item['relative_path'] not in known)
                self.dependency_label.set(', '.join(item['relative_path'] for item in self.dependencies) or '未添加依赖文件')
                self.invalidate()
            except ValueError as exc:
                messagebox.showerror('无法添加依赖', str(exc))

        def clear_dependencies(self):
            self.dependencies = []
            self.dependency_label.set('未添加依赖文件')
            self.invalidate()

        def open_output(self):
            directory = Path(self.output_dir.get()).expanduser().resolve()
            if not directory.is_dir():
                messagebox.showinfo('尚未生成', '请先规划并保存任务，输出目录会自动创建。')
                return
            if os.name == 'nt':
                os.startfile(str(directory))
            else:
                import webbrowser
                webbrowser.open(directory.as_uri())

        def append_log(self, value):
            self.log.configure(state='normal')
            self.log.insert('end', value)
            self.log.see('end')
            self.log.configure(state='disabled')

        def set_running(self, running):
            if running:
                self.disabled_widgets = []
                def disable(widget):
                    for child in widget.winfo_children():
                        disable(child)
                    try:
                        state = widget.cget('state')
                        if state != 'disabled':
                            self.disabled_widgets.append((widget, state))
                            widget.configure(state='disabled')
                    except tk.TclError:
                        pass
                for panel in self.edit_panels:
                    disable(panel)
            else:
                for widget, state in self.disabled_widgets:
                    try:
                        widget.configure(state=state)
                    except tk.TclError:
                        pass
                self.disabled_widgets = []
            for button in self.task_buttons:
                button.configure(state='disabled' if running else 'normal')
            self.stop_button.configure(state='normal' if running else 'disabled')

        def save_task(self):
            self.ensure_idle()
            if not self.generate():
                return None
            directory = Path(self.bundle['output_dir'])
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / 'task.json'
            if target.exists():
                existing = json.loads(target.read_text(encoding='utf-8-sig'))
                if existing != self.bundle:
                    raise ValueError('此输出目录已有不同的 task.json。请选择新的输出目录，避免覆盖历史任务。')
            else:
                write_json_new(target, self.bundle)
            return target

        def launch_task(self, command, retry=False):
            try:
                task = self.save_task()
                if task is None:
                    return
                if self.stop_path is not None and self.stop_path.exists():
                    self.stop_path.unlink()
                self.stop_path = task.parent / ('.gui-stop-request-' + uuid.uuid4().hex)
                arguments = [sys.executable, '-u', str(ROOT / 'run_experiments.py'), command,
                             '--config', str(task), '--stop-file', str(self.stop_path)]
                if retry:
                    arguments.append('--retry-failed')
                environment = os.environ.copy()
                environment['PYTHONIOENCODING'] = 'utf-8'
                environment['PYTHONUTF8'] = '1'
                self.process = subprocess.Popen(arguments, cwd=str(ROOT), env=environment,
                                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                text=True, encoding='utf-8', errors='replace',
                                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                self.active_command = command
                process = self.process
                self.set_running(True)
                if command == 'plan':
                    self.status.set('正在规划任务…')
                elif command == 'preflight':
                    self.status.set('PRECHECK：仅创建 CAD 模型并检查几何，不执行 FDTD 求解。')
                else:
                    self.status.set('队列运行中；模型、求解与分析严格由原 LSF 执行。')
                label = 'PRECHECK (preflight)' if command == 'preflight' else command
                self.append_log(f'\n>>> {label}  {task}\n')
                def read_output():
                    try:
                        for line in process.stdout:
                            self.events.put(('log', line))
                        self.events.put(('exit', process.wait()))
                    except Exception as exc:
                        self.events.put(('log', f'日志读取失败：{exc}\n'))
                        self.events.put(('exit', process.wait()))
                    finally:
                        process.stdout.close()
                threading.Thread(target=read_output, daemon=True).start()
            except (ValueError, OSError) as exc:
                messagebox.showerror('无法启动任务', str(exc))

        def poll_process(self):
            try:
                while True:
                    kind, value = self.events.get_nowait()
                    if kind == 'log':
                        self.append_log(value)
                    elif kind == 'exit':
                        self.process = None
                        self.set_running(False)
                        self.append_log(f'\n队列已退出，退出码 {value}。\n')
                        if value == 0 and self.active_command == 'plan':
                            self.status.set('规划完成；task.json 和各案例 script.lsf 已保存，尚未运行 Lumerical。可启动队列。')
                        elif value == 0 and self.active_command == 'preflight':
                            self.status.set('建模预检完成；请查看输出目录 preflight/<时间戳>/report.html。未求解，不代表物理结果通过。')
                        elif self.active_command == 'preflight':
                            self.status.set('建模预检未通过或已停止；请查看 PRECHECK 日志与 preflight 下的报告，修正后重新预检。')
                        elif value == 0:
                            self.status.set('任务结束；请查看输出目录的 report.html 和案例日志。')
                        else:
                            self.status.set('任务已停止或存在失败；请查看日志，修正原因后重试失败案例。')
                        self.active_command = None
                        if self.closing:
                            self.window.destroy()
                            return
            except queue.Empty:
                pass
            self.window.after(150, self.poll_process)

        def request_stop(self):
            if self.active() and self.stop_path:
                try:
                    self.stop_path.write_text('stop requested by GUI\n', encoding='utf-8')
                    self.stop_button.configure(state='disabled')
                    self.status.set('已请求停止；等待队列结束自己启动的 worker 和 Lumerical 会话…')
                except OSError as exc:
                    messagebox.showerror('无法提交停止请求', str(exc))

        def close(self):
            if not self.active():
                self.window.destroy()
                return
            if messagebox.askyesno('停止当前任务', '当前窗口还有任务运行。是否请求停止，待清理完成后关闭窗口？'):
                self.closing = True
                self.request_stop()

        def invalidate(self):
            self.bundle = None
            if hasattr(self, 'preview'):
                self.show_text(self.preview, '')

        def source_changed(self, _event=None):
            if self.source.edit_modified():
                self.source.edit_modified(False)
                if not self.loading:
                    self.invalidate()
                    self.status.set('脚本文本已修改；请重新解析参数后生成 JSON。')

        def open_file(self):
            path = filedialog.askopenfilename(title='选择 LSF 文件',
                                              filetypes=[('Lumerical 脚本', '*.lsf'), ('所有文件', '*.*')])
            if path:
                self.load_file(path)

        def load_file(self, path):
            try:
                self.ensure_idle()
                script, encoding = read_lsf(path)
                self.filename = Path(path).name
                self.source_dir = Path(path).resolve().parent
                self.output_dir.set(str(ROOT / f'output_lsf_{Path(path).stem}'))
                self.name.set(f'{Path(path).stem}_scan')
                self.clear_dependencies()
                self.loading = True
                self.source.delete('1.0', 'end')
                self.source.insert('1.0', script)
                self.source.edit_modified(False)
                self.loading = False
                self.source_label.set(f'{self.filename}（{encoding}）')
                self.parse()
            except (ValueError, OSError) as exc:
                self.loading = False
                messagebox.showerror('无法导入', str(exc))

        def parse(self):
            if self.active():
                return
            self.invalidate()
            self.snapshot = None
            self.inspection = None
            self.selected_id = None
            self.fixed = {}
            self.rows = {}
            self.target.set('尚未选择')
            for row in self.table.get_children():
                self.table.delete(row)
            try:
                script = self.source.get('1.0', 'end-1c')
                inspection = inspect_lsf(script)
                self.snapshot = script
                self.inspection = inspection
                for index, parameter in enumerate(inspection['parameters']):
                    row = f'p{index}'
                    self.rows[row] = parameter
                    self.table.insert('', 'end', iid=row, text=parameter['label'])
                self.refresh_rows()
                notices = [f'维度识别：{inspection.get("dimension", "未知")}',
                           '只识别受支持的数值赋值；不会解释脚本逻辑或确认物理结构。']
                notices.extend(inspection.get('warnings', []))
                self.show_text(self.warnings, '\n'.join(notices))
                count = sum(bool(p['editable'] and p.get('exposed')) for p in inspection['parameters'])
                self.status.set(f'解析完成：{count} 个可编辑数值。选择一个扫描参数；重新解析会清空此前选择和固定值。')
            except ValueError as exc:
                self.show_text(self.warnings, str(exc))
                self.status.set('解析失败，请修正脚本后重试。')
                messagebox.showerror('无法解析', str(exc))

        def ensure_parsed(self):
            if self.snapshot is None or self.inspection is None:
                raise ValueError('请先导入或粘贴 LSF 并解析参数。')
            if self.source.get('1.0', 'end-1c') != self.snapshot:
                raise ValueError('脚本在上次解析后发生变化。请重新解析并选择参数，避免修改错误的赋值位置。')

        def row_parameter(self):
            self.ensure_idle()
            self.ensure_parsed()
            selected = self.table.selection()
            if not selected:
                raise ValueError('请先在参数列表中选择一行。')
            parameter = self.rows[selected[0]]
            if not parameter['editable']:
                raise ValueError(f'该项不可编辑：{parameter.get("reason", "不支持的赋值")}')
            if not parameter.get('exposed'):
                raise ValueError(f'该项不在可暴露输入区：{parameter.get("exposure_reason", "请将输入提取到参数区")}')
            return parameter

        def refresh_rows(self):
            for row, parameter in self.rows.items():
                identifier = parameter['id']
                state = '可编辑'
                if not parameter['editable']:
                    state = parameter.get('reason', '不可编辑')
                elif not parameter.get('exposed'):
                    state = parameter.get('exposure_reason', '不在输入区')
                elif identifier == self.selected_id:
                    state = '扫描参数'
                elif identifier in self.fixed:
                    state = '固定值'
                self.table.item(row, values=(parameter['line'], parameter['value'],
                                            self.fixed.get(identifier, ''), state),
                                tags=() if parameter['editable'] and parameter.get('exposed') else ('locked',))

        def choose_target(self):
            try:
                parameter = self.row_parameter()
                if parameter['id'] in self.fixed:
                    raise ValueError('该参数已设置固定值。请先取消固定值，再设为扫描参数。')
                self.selected_id = parameter['id']
                self.target.set(f'{parameter["label"]}（第 {parameter["line"]} 行；原值 {parameter["value"]}）')
                self.refresh_rows()
                self.invalidate()
            except ValueError as exc:
                messagebox.showerror('无法选择', str(exc))

        def set_fixed(self):
            try:
                parameter = self.row_parameter()
                if parameter['id'] == self.selected_id:
                    raise ValueError('扫描参数不能同时设置固定值。请先选择另一个扫描参数。')
                self.fixed[parameter['id']] = single_value(self.fixed_input.get())
                self.refresh_rows()
                self.invalidate()
            except ValueError as exc:
                messagebox.showerror('无法设置固定值', str(exc))

        def clear_fixed(self):
            try:
                parameter = self.row_parameter()
                self.fixed.pop(parameter['id'], None)
                self.refresh_rows()
                self.invalidate()
            except ValueError as exc:
                messagebox.showerror('无法取消固定值', str(exc))

        def scan_values(self):
            if self.scan_mode.get() == 'values':
                return parse_values(self.values_input.get())
            start, stop = single_value(self.start.get()), single_value(self.stop.get())
            if self.spacing_mode.get() == 'count':
                try:
                    count = int(self.spacing.get().strip())
                except ValueError as exc:
                    raise ValueError('点数 count 必须是整数，例如 5。') from exc
                return range_values(start, stop, count=count)
            return range_values(start, stop, step=single_value(self.spacing.get()))

        def generate(self):
            try:
                self.ensure_idle()
                self.invalidate()
                self.ensure_parsed()
                if self.selected_id is None:
                    raise ValueError('请先选择一个扫描参数。')
                # Reinspect the exact saved snapshot in the core on each export.
                self.bundle = make_bundle(self.snapshot, self.filename, self.selected_id,
                                          self.scan_values(), fixed_values=dict(self.fixed),
                                          name=self.name.get().strip())
                if not self.output_dir.get().strip():
                    raise ValueError('请选择输出目录。')
                minutes = single_value(self.timeout.get())
                if minutes < 1 or int(minutes) != minutes:
                    raise ValueError('案例超时分钟数必须为大于等于 1 的整数。')
                self.bundle.update(output_dir=str(Path(self.output_dir.get()).expanduser().resolve()),
                                   lumerical_api_python=self.api_dir.get().strip(), max_case_minutes=minutes,
                                   execution={'mode': 'script', 'input_files': list(self.dependencies),
                                              'required_outputs': [item.strip() for item in self.required_outputs.get().replace('\n', ',').split(',') if item.strip()]})
                if self.source_dir is not None:
                    self.bundle['source']['source_dir'] = str(self.source_dir)
                self.show_text(self.preview, json.dumps(self.bundle, ensure_ascii=False,
                                                       indent=2, allow_nan=False))
                self.status.set(f'已生成 {len(self.bundle["sweep"]["values"])} 个扫描值的 JSON；未执行 LSF。')
                return True
            except ValueError as exc:
                messagebox.showerror('无法生成 JSON', str(exc))
                return False

        def copy(self):
            if self.generate():
                self.window.clipboard_clear()
                self.window.clipboard_append(json.dumps(self.bundle, ensure_ascii=False,
                                                        indent=2, allow_nan=False))
                self.status.set('JSON 已复制到剪贴板。')

        def save(self):
            if not self.generate():
                return
            destination = ROOT / 'lsf_exports'
            try:
                destination.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror('无法创建输出目录', str(exc))
                return
            path = filedialog.asksaveasfilename(title='另存 JSON（请选择新文件名）',
                                                initialdir=str(destination),
                                                initialfile=f'{Path(self.filename).stem}_scan.json',
                                                defaultextension='.json',
                                                filetypes=[('JSON 文件', '*.json')])
            if not path:
                return
            try:
                saved = write_json_new(path, self.bundle)
                self.status.set(f'已保存：{saved}。原始 LSF 未修改，未执行脚本。')
            except FileExistsError:
                messagebox.showerror('文件已存在', '为保留已有配置，请选择一个新的文件名。')
            except (ValueError, OSError) as exc:
                messagebox.showerror('无法保存', str(exc))

    window = tk.Tk()
    editor = Editor(window)
    if initial_lsf:
        window.after(0, lambda: editor.load_file(initial_lsf))
    window.mainloop()


def build_parser():
    parser = argparse.ArgumentParser(description='完整 LSF 参数扫描与求解队列；inspect/export/expand 子命令仅处理文本。')
    commands = parser.add_subparsers(dest='command')
    gui = commands.add_parser('gui', help='打开中文图形界面（不带子命令时默认打开）')
    gui.add_argument('--lsf', help='打开界面后导入此 LSF 文件')
    inspect = commands.add_parser('inspect', help='静态列出可编辑参数及限制')
    inspect.add_argument('--lsf', required=True, help='输入 LSF 文件')
    export = commands.add_parser('export', help='导出一个参数的扫描 JSON；拒绝覆盖已有文件')
    export.add_argument('--lsf', required=True, help='输入 LSF 文件')
    export.add_argument('--parameter', required=True, help='inspect 中的精确标签，例如 film_t_nm(1)')
    choice = export.add_mutually_exclusive_group(required=True)
    choice.add_argument('--values', help='显式数值，例如 100,200,300')
    choice.add_argument('--start', help='范围起点，单位沿用 LSF')
    export.add_argument('--stop', help='范围终点')
    spacing = export.add_mutually_exclusive_group()
    spacing.add_argument('--step', help='步长，不能为零')
    spacing.add_argument('--count', type=int, help='包含两端的采样点数')
    export.add_argument('--set', dest='fixed', action='append', default=[], metavar='LABEL=VALUE',
                        help='其他参数的固定值，可重复；不能是扫描参数')
    export.add_argument('--name', default='lsf_scan', help='任务名称，默认 lsf_scan')
    export.add_argument('--output', required=True, help='新的 .json 输出文件')
    expand = commands.add_parser('expand', help='展开 JSON 为逐案例 LSF 文本文件，不执行')
    expand.add_argument('--config', required=True, help='lsf-sweep-1.0 JSON 文件')
    expand.add_argument('--output-dir', required=True, help='输出目录；拒绝覆盖已有 case_*.lsf 文件')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command in (None, 'gui'):
        try:
            launch_gui(getattr(args, 'lsf', None))
            return 0
        except ImportError:
            print('无法启动界面：当前 Python 缺少 tkinter。请使用包含 Tk 的 Python 或使用 CLI 子命令。', file=sys.stderr)
            return 2
        except Exception as exc:
            print(f'无法启动或运行图形界面：{exc}', file=sys.stderr)
            return 2
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        run_cli(args)
        return 0
    except (ValueError, OSError) as exc:
        print(f'错误：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
