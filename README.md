# AH53 Experiment Runner v2：一步一步使用教程

这个程序帮你做这样一件事：**给定一组 AH53 光栅参数，自动建立 Lumerical 模型，逐个计算，再整理成图表和 Excel 可以读取的数据。**

例如，你想知道“平坦化覆盖膜从 0 nm 增加到 400 nm，反射强度和相位如何变化”。在配置文件中填写厚度列表后，程序会为每个厚度建立独立模型、计算 s/p 偏振、保存结果并生成网页报告。

**程序目前通过 PowerShell 命令操作，没有独立图形界面。** 计算由本机安装的 Lumerical 完成，结果用浏览器查看。第一次使用可以先不读源码、Jones 矩阵和 RCWA，按下面步骤完成三点小实验。

已实际验证的环境：**Windows、Lumerical 2025 R2（8.34.4175）、其自带 Python 3.13.1**。原始目标版本 2023 R2.1 尚未实际验证。

**已加入 MDF 材料库支持。** 基底、条纹和每层膜可填写完整材料名，无需手抄 n、k。按 [MDF 材料逐步教程](docs/MDF材料逐步教程.md) 操作；已完成的双后端平面验证和按用户要求停止的带膜 RCWA 后续验收，分别记录在 [材料验收记录](docs/MDF材料验收记录.md)。

## 阅读路线

| 你想做什么 | 从哪里开始 |
|---|---|
| 在原电脑看已经算好的结果 | [先看现有结果](#existing-results) |
| 第一次学习运行程序 | 按[第 1 步](#step-1)至[第 7 步](#step-7)顺序操作 |
| 换电脑或从 GitHub 下载 | [第 1 步](#step-1)获取代码，再完成[第 3 步](#step-3)的路径配置 |
| 自己修改膜厚、线宽、高度 | [第 8 步：自己的实验](#step-8) |
| 使用自己的 `.mdf` 材料库、填写材料名 | [MDF 材料逐步教程](docs/MDF材料逐步教程.md) |
| 执行完整 39 点扫描 | [第 9 步：批量计算](#step-9) |
| 中断或报错 | [续跑与故障排查](#troubleshooting) |
| RCWA、精度与技术定义 | [进阶说明](#advanced) |

<a id="existing-results"></a>

## 先看现有结果：原电脑上可以直接打开

如果使用的是此前完成验收的电脑，项目仍在 `D:\工作\AH53_Experiment_Runner_v2`，可以在文件资源管理器中双击：

| 文件 | 能看到什么 |
|---|---|
| `output_ah53_quick\report.html` | 覆盖膜 0、200、400 nm 的三个结果，适合先熟悉界面 |
| `output_ah53_overnight\report.html` | 8 组、39 个实验点的报告 |
| `output_ah53_physics_validation\physics_validation.html` | 空气、平面界面、对称和位移的物理检查 |

用 Edge 或 Chrome 打开即可，无需启动 Lumerical。读图方法见[第 7 步](#step-7)。

**刚从 GitHub 下载时没有这些结果目录。** 仓库包含程序、配置和验收摘要；完整仿真数据保留在原电脑，新电脑需要运行生成。

<a id="step-1"></a>

## 第 1 步：找到项目文件夹，在这里打开 PowerShell

原电脑已有项目时，直接使用 `D:\工作\AH53_Experiment_Runner_v2`，不需要重新下载。

如果还没有代码：打开[GitHub 仓库](https://github.com/ychen1213/AH53_Experiment_Runner_v2)，点击 **Code → Download ZIP**，解压到一个可写文件夹。进入包含 `run_experiments.py` 的那一层；下载后的文件夹名可能带 `-main`。不要在 ZIP 压缩包内部运行。

然后：

1. 在文件资源管理器中打开项目文件夹。
2. 点击上方地址栏，输入 **powershell**，按回车。
3. 在出现的命令窗口中输入下面这行，按回车：

~~~powershell
Get-Location
~~~

原电脑应显示 `D:\工作\AH53_Experiment_Runner_v2`。如果位置不对，输入：

~~~powershell
Set-Location -LiteralPath 'D:\工作\AH53_Experiment_Runner_v2'
~~~

其他电脑使用实际解压目录。再检查当前文件夹：

~~~powershell
Test-Path .\run_experiments.py
~~~

**应显示 True。** 如果是 False，请进入真正包含该文件的目录后再继续。

下文命令都在这个 PowerShell 窗口中运行。复制代码框内的命令，不要复制代码框边缘，也不要粘贴进 Lumerical 的 Script Prompt。每一条执行结束、重新出现 `PS ...>` 提示符后，再运行下一条。

<a id="step-2"></a>

## 第 2 步：告诉 PowerShell 使用哪个 Python

在已验收的原电脑上，复制执行：

~~~powershell
$lumPython = 'D:\Ansys Inc\v252\Lumerical\python\python.exe'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
~~~

第一行给解释器路径起一个方便使用的变量名，不会开始计算。后两行让中文输入输出使用 UTF-8。

检查路径：

~~~powershell
Test-Path -LiteralPath $lumPython
~~~

应显示 True。然后检查解释器和依赖：

~~~powershell
& $lumPython -c "import sys, numpy, matplotlib; print(sys.executable); print(sys.version); print('numpy', numpy.__version__); print('matplotlib', matplotlib.__version__)"
~~~

原电脑会显示 Lumerical 安装目录中的 Python，以及 Python 3.13.1、NumPy 2.2.2、Matplotlib 3.10.0。

- **&** 是 PowerShell 的调用符号，表示“运行后面的程序”，请保留。
- **$lumPython** 是上面定义的路径。关闭窗口后需要重新定义它，也就是重新执行本步骤。
- 换电脑时，把第一行改为实际的 Python 路径；可在 Lumerical 安装目录的 `python` 子文件夹查找。
- 本机自带环境已经有这些依赖，**无需执行 pip 安装**。[requirements-v252.txt](requirements-v252.txt) 记录已验证版本；早期的 requirements.txt 不适用于这套 Python 3.13 环境。

本教程统一使用 `& $lumPython`，因为原电脑直接输入 python 会命中 WindowsApps 的空别名。

<a id="step-3"></a>

## 第 3 步：检查 Lumerical API 路径

Python 负责调度，lumapi.py 负责让 Python 控制 Lumerical。这是两个不同的路径：

| 配置位置 | 应填写什么 | 原电脑的值 |
|---|---|---|
| PowerShell 的 $lumPython | 完整的 python.exe 文件路径 | `D:\Ansys Inc\v252\Lumerical\python\python.exe` |
| 实验 JSON 的 lumerical_api_python | **包含 lumapi.py 的文件夹** | `D:/Ansys Inc/v252/Lumerical/api/python` |

在原电脑运行：

~~~powershell
Test-Path -LiteralPath 'D:\Ansys Inc\v252\Lumerical\api\python\lumapi.py'
~~~

显示 True 且安装位置没变化时，现有实验配置可以直接使用。

**如果换了电脑或安装位置不同：** 用记事本打开配置：

~~~powershell
notepad .\configs\quick_check.json
~~~

找到这一行，将引号内路径改为实际目录并保存：

~~~json
"lumerical_api_python": "D:/Ansys Inc/v252/Lumerical/api/python"
~~~

C 盘安装路径可能类似 `C:/Program Files/ANSYS Inc/v252/Lumerical/api/python`，请以实际存在的文件为准。**这一项不要写到 lumapi.py 文件本身。** JSON 内路径建议用正斜杠 /；反斜杠需要写成两个，例如 `D:\\Ansys Inc\\v252`。

同样修改接下来会用的 **configs/physics_validation.json** 和 **configs/overnight_example.json**。使用教学配置或 RCWA 时，也要修改相应 JSON。每个实验文件独立读取自己的 API 路径。

真实计算需要 Lumerical 的有效许可证。可以先手动打开 Lumerical 确认能正常求解；离线检查不会验证求解许可证。

**可选：使用 .bat 快捷启动器时的 Python 配置。** 原电脑已配置好。新电脑可把 configs/local_runtime.example.json 复制为 configs/local_runtime.json，编辑其中的 python_executable。这个本机文件不上传 GitHub。它只选择启动器的 Python，不会修改实验 JSON 的 API 路径；直接使用 $lumPython 时不依赖它。

<a id="step-4"></a>

## 第 4 步：先预览三个实验点

本步骤生成任务和几何预览，不启动 Lumerical 求解：

~~~powershell
& $lumPython run_experiments.py plan --config configs/quick_check.json
~~~

这条命令的意思是：用 quick_check.json 作为实验说明书，生成计划。首次使用应看到类似：

~~~text
CONFIG: ...\configs\quick_check.json
OUT: ...\output_ah53_quick
CASES: 3
  film_cap_quick_... => films.0.thickness_nm = 0 ...
  film_cap_quick_... => films.0.thickness_nm = 200 ...
  film_cap_quick_... => films.0.thickness_nm = 400 ...
STATE: {'DONE': 0, 'FAILED': 0, 'RUNNING': 0, 'PENDING': 3}
NO SOLVER RUNS WERE PERFORMED.
~~~

每个实验点称为一个 **case**。末尾的长编号是自动生成的标识，不需要手工填写。原电脑已有完整结果时，状态会显示 DONE: 3。

打开输出文件夹：

~~~powershell
Invoke-Item .\output_ah53_quick
~~~

进入任一个 film_cap_quick_... 子目录，双击 **geometry_preview.png**，确认结构符合预期。这个图只是几何示意，尚不表示求解成功。

三个点只改变**平坦化膜的顶部厚度**。共同参数为：

| 参数 | 默认值 | 含义 |
|---|---|---|
| wavelength_nm | 643 nm | 入射光波长 |
| period_nm | 16000 nm | 整个结构的母周期 |
| child_pitch_nm | 3200 nm | 相邻条纹中心间距，母周期中有三条条纹 |
| ridge_cd_nm | 1600 nm | 每条条纹宽度 |
| ridge_height_nm | 300 nm | 条纹高度 |
| n_substrate / n_ridge | 1.5 / 2.0 | 基底 / 条纹折射率 |
| films 第一层的 n | 1.46 | 覆盖膜折射率 |

**这里的 0 nm 仍填满条纹间的沟槽，只是没有高于条纹顶部的额外膜厚。** 200 nm 时最上表面在 Y=500 nm，400 nm 时在 Y=700 nm。真正的裸光栅应配置 `"films": []`，两者的结构和结果不同。

还可以运行离线回归测试，确认程序文件和依赖：

~~~powershell
& $lumPython -m unittest discover -s tests -v
~~~

应以 **Ran … tests** 和 **OK** 结束，测试数量会随功能更新。这验证程序逻辑，不替代真实电磁仿真验收。

<a id="step-5"></a>

## 第 5 步：只计算一个实验点

本步骤会真实调用 Lumerical：

~~~powershell
& $lumPython run_experiments.py run --config configs/quick_check.json --limit 1 --visible
~~~

| 部分 | 含义 |
|---|---|
| run | 开始真实计算 |
| --config configs/quick_check.json | 使用三点小实验配置 |
| --limit 1 | 本次最多实际执行一个需要运行的 case |
| --visible | 显示 Lumerical 窗口，方便观察首次运行 |

一个 case 内部进行 **4 次 FDTD 求解**：空气参考 s、空气参考 p、带结构 s、带结构 p。因此“只跑一个点”也会经历多个阶段。让程序自己控制并关闭窗口，等待 PowerShell 返回提示符。

首次成功时会看到类似：

~~~text
START film_cap_quick_...
OK film_cap_quick_... 55.8s
FINISHED: {'DONE': 1, 'FAILED': 0, 'RUNNING': 0, 'PENDING': 2}
Report: ...\output_ah53_quick\report.html
~~~

时间只是本机历史量级，许可证、硬件和参数都会影响耗时。max_case_minutes 是单个 case 的超时上限，不是承诺的运行时间。

**如果看到 SKIP ... DONE 而没有弹出窗口，是在复用已完成且校验有效的结果。** 原电脑的 quick 三点已算过，本命令通常全部跳过。想亲手观察新的计算，请用[第 8 步](#step-8)的独立教学配置。程序没有 --force 参数。

出现 FAILED 时，先按[故障排查](#troubleshooting)查看对应 worker.log，解决后重试，不要直接进入大批量计算。

<a id="step-6"></a>

## 第 6 步：通过物理检查，再完成三个点

先计算四个用于检查物理结果的基准案例：

~~~powershell
& $lumPython run_experiments.py run --config configs/physics_validation.json
~~~

等待结束，确认 DONE: 4、FAILED: 0。然后读取真实结果做检查：

~~~powershell
& $lumPython tools/validate_physics.py
~~~

应看到 **PHYSICS VALIDATION: PASS**。打开检查页面：

~~~powershell
Invoke-Item .\output_ah53_physics_validation\physics_validation.html
~~~

检查包括空气中反射接近零、平面界面满足 Fresnel 公式、对称光栅的 ±m 级结果、整体平移后的相位变化。**validate_physics.py 只检查已有结果，缺少计算结果时不会自动补算。**

通过后，计算 quick 中剩余的点：

~~~powershell
& $lumPython run_experiments.py run --config configs/quick_check.json
~~~

这次没有 --limit 1，会顺序处理尚未开始的点，完整有效的已完成点自动跳过。此前有失败项时，需要按[续跑说明](#troubleshooting)显式重试。

最终应看到：

~~~text
FINISHED: {'DONE': 3, 'FAILED': 0, 'RUNNING': 0, 'PENDING': 0}
~~~

run 正常结束会自动生成报告。以后如果只是重新整理已有数据，使用：

~~~powershell
& $lumPython run_experiments.py report --config configs/quick_check.json
~~~

report 不启动求解，也不会补算缺失结果。配置或数学源码发生变化时，旧结果可能失效，plan、run、report 的准备阶段也可能归档旧现场；保留旧实验时请同时保留其原始配置和源码版本。

<a id="step-7"></a>

## 第 7 步：打开报告，读懂第一组结果

~~~powershell
Invoke-Item .\output_ah53_quick\report.html
~~~

浏览器中的阅读顺序：

1. **任务汇总**：确认本组为 3/3 完成。DONE 表示计算和文件完整性通过，不表示任意精度目标都已满足。
2. **质量检查警告**：若存在警告，检查能量闭合、通量或对称性问题；没有警告也不能替代网格收敛分析。
3. **对照图**：大多数扫描图的横轴为本组参数，本例为膜顶厚度；曲线对应不同衍射阶或偏振通道。
4. **完整任务清单**：查看具体 case 的状态，失败时从这里找日志。

### 先认识图中的几个词

| 名称 | 如何理解 |
|---|---|
| m，如 0、+1、−1、+5、−5 | 衍射阶；0 级是镜面反射方向，其他阶对应不同出射方向 |
| s / p | 两种入射偏振；当前二维模型中 s 沿 Z，入射 p 沿 X |
| rss / rpp | s 入射到 s 出射、p 入射到 p 出射的复电场系数 |
| rss amplitude / rpp amplitude | 复电场系数的模，看幅值随参数如何变化 |
| ±m relative phase | 同一偏振中 +m 与 −m 的相对相位，单位度；不是 s 与 p 的相位差 |
| reflected power | 所绘衍射阶的反射功率比例随参数的变化 |
| complex plane | 复系数变化轨迹，横轴实部、纵轴虚部；首次使用可以稍后看 |
| 近场图 | 归一化电场强度，用于观察结构附近的场分布与局部增强 |

**电场幅值与反射功率不是同一列数据。** 要回答“多少入射功率进入 +1 级”，优先看 eta_s 或 eta_p。例如 eta_s = 0.02 表示 s 入射时该阶占入射功率的 2%。

### 在 Excel 中查看具体数值

打开 **output_ah53_quick/coefficients_long.csv**。一行对应“一个 case 的一个衍射阶”，所以三个实验点有很多行，当前默认配置为 147 行数据，不含表头。

| 列名 | 含义 |
|---|---|
| group、parameter、value | 实验组、扫描字段、当前参数值 |
| m、theta_deg | 衍射阶和出射角度 |
| eta_s、eta_p | s/p 入射时进入该反射阶的功率比例 |
| rss_real、rss_imag | s→s 复电场系数的实部、虚部 |
| rss_abs、rss_phase_deg | s→s 的模、相位（度） |
| rpp_* | p→p 的对应数据 |
| rsp_*、rps_* | 交叉偏振通道；第一个字母是输出，第二个是输入 |
| *_source | 系数来源，如 fdtd_fields 或 analytic_by_2d_symmetry |

**具体操作：** 在 Excel 中选中表头，开启“数据 → 筛选”，把 m 筛选为 1。value 为 0、200、400 的三行就是三个膜厚的 +1 级结果。用 value 作横轴、eta_s 作纵轴，可以画出 s 偏振 +1 级效率曲线。

例如原电脑的粗网格结果中，膜顶厚 200 nm、m=+5 的 rss_abs 约为 0.01925，而 eta_s 约为 0.0003632，即 **0.03632% 的入射功率**。不能把幅值 0.01925 直接当成 1.925% 功率。这个数值仅用于解释读法，不是精度保证。

接近零振幅时，相位列可能为空，表示相位不可靠，不要补成 0°。当前各向同性二维模型的交叉项按偏振解耦设零，并在来源列标明；不能据此推断真实三维样品没有偏振转换。

### 哪些文件需要保留

~~~text
output_ah53_quick/
  report.html                  浏览器报告入口
  coefficients_long.csv        Excel 可读的逐阶数据
  status_overview.csv          每个 case 的状态、耗时与错误
  quality_warnings.csv         质量预警
  manifest.json                本次配置的任务清单
  figures/                     报告引用的图片
  film_cap_quick_.../           一个 case 的独立文件夹
    case.json                  该点的冻结输入
    geometry_preview.png       几何示意图
    geometry.json              几何数据
    AH53_model.fsp             Lumerical 工程
    worker.log                 该点运行日志
    status.json                该点执行状态
    result.npz                 Python 可读的复数结果
    physics_metadata.json      数值约定与结果来源
    attempts/                  重试时归档的旧现场（若有）
~~~

不同状态的 case 文件数量不同，**不能只凭一个 .fsp 或 .npz 文件存在就判为成功**。近场只在配置选中的代表点保存，默认每组最多三个，未选中的点没有近场属于正常情况。

移动或分享报告时，复制整个输出文件夹；单独复制 report.html 会丢失图片和数据链接。GitHub 的源文件页面不能代替本机 HTML 报告。

<a id="step-8"></a>

## 第 8 步：亲手修改一次实验——五个膜厚点

仓库附带一个独立教学配置：[configs/tutorial_film_scan.json](configs/tutorial_film_scan.json)。它使用与 quick 相同的基础模型，扫描 **0、100、200、300、400 nm**，结果写到新的 **output_ah53_tutorial** 文件夹。

在原电脑上，这可以让你观察一次新的实际计算，同时保留之前 quick 的结果。新电脑先把教学配置的 lumerical_api_python 改为实际路径。

### 8.1 打开配置，认识四个关键字段

~~~powershell
notepad .\configs\tutorial_film_scan.json
~~~

| 字段 | 教学配置中的值 | 用途 |
|---|---|---|
| output_dir | ../output_ah53_tutorial | 结果保存位置 |
| base_model | 光栅、材料、波长、网格等参数 | 所有点共用的基础结构 |
| experiments[0].parameter | films.0.thickness_nm | 本组只改变第一层膜厚 |
| experiments[0].values | [0, 100, 200, 300, 400] | 要计算的五个厚度 |

films.0.thickness_nm 中的 0 表示膜列表的第一层。output_dir 的相对路径**以配置文件所在目录为起点**；配置在 configs 下，所以 ../output_ah53_tutorial 指向项目根目录中的结果文件夹。

先用随附配置走通一次，以后可以修改 values。例如 [100, 200, 300] 就只扫描这三个厚度。JSON 使用英文双引号和英文逗号，最后一项后面不加逗号，不支持 // 注释。另存时选择 **UTF-8（不带 BOM）**，保留 .json 扩展名；不要另存为 .json.txt。

### 8.2 先看计划，再跑一个点

~~~powershell
& $lumPython run_experiments.py plan --config configs/tutorial_film_scan.json
~~~

应显示 CASES: 5，OUT 指向 output_ah53_tutorial。确认几何预览后：

~~~powershell
& $lumPython run_experiments.py run --config configs/tutorial_film_scan.json --limit 1 --visible
~~~

成功后，再运行剩下的点：

~~~powershell
& $lumPython run_experiments.py run --config configs/tutorial_film_scan.json
~~~

最终应为 DONE: 5、FAILED: 0、PENDING: 0。打开结果：

~~~powershell
Invoke-Item .\output_ah53_tutorial\report.html
~~~

这份配置仍使用粗网格。它是练习流程和参数扫描的示例，不新增任何精度验收结论。重复执行时也会跳过有效结果。

### 8.3 下一次想改线宽、高度或网格怎么办

先复制一个配置，用新文件名保存，并修改 output_dir 为新的输出目录，例如 ../output_ah53_cd_study。这样容易区分实验并保留对照结果：

~~~powershell
Copy-Item -LiteralPath .\configs\tutorial_film_scan.json -Destination .\configs\my_cd_scan.json
notepad .\configs\my_cd_scan.json
~~~

如果扫描**裸光栅线宽**，做三处修改：

1. 把 output_dir 改为 ../output_ah53_cd_study。
2. 把 base_model 中的 films 改成空数组，即下面这一项：

~~~json
"films": []
~~~

3. 把整个 experiments 数组改成：

~~~json
"experiments": [
  {
    "name": "my_cd_scan",
    "parameter": "ridge_cd_nm",
    "values": [1200, 1600, 2000]
  }
]
~~~

以上两个代码框都是配置片段，不是完整文件；保留原文件中其他字段和字段之间的逗号。保存后：

~~~powershell
& $lumPython run_experiments.py plan --config configs/my_cd_scan.json
~~~

应显示 CASES: 3，OUT 指向 output_ah53_cd_study。确认预览是裸光栅后：

~~~powershell
& $lumPython run_experiments.py run --config configs/my_cd_scan.json --limit 1 --visible
~~~

成功后执行其余点：

~~~powershell
& $lumPython run_experiments.py run --config configs/my_cd_scan.json
Invoke-Item .\output_ah53_cd_study\report.html
~~~

其他常用参数：

| 想改变什么 | parameter | 示例 values | 备注 |
|---|---|---|---|
| 膜顶厚度 | films.0.thickness_nm | [0, 100, 200] | films 中必须有第一层膜 |
| 线宽 | ridge_cd_nm | [1200, 1600, 2000] | 单位 nm，条纹中心固定 |
| 条纹高度 | ridge_height_nm | [100, 200, 300, 400] | 单位 nm |
| 整体横向位移 | ridge_shift_nm | [-100, 0, 100] | 单位 nm，观察各阶相位变化 |
| X 网格步长 | mesh.dx_nm | [70, 50, 35, 25] | mesh.mode 必须为 override，Y 步长仍固定 |
| 自动网格等级 | mesh.mesh_accuracy | [2, 3, 4] | mesh.mode 必须改为 auto |

每组相对于 base_model 独立展开；overrides 是该组固定的覆盖参数。**多组扫描不会自动组合所有参数。** 例如三种厚度和三种线宽写成两个独立组，会得到 6 个点，而非 9 个组合。

材料可以继续使用恒定实折射率（数值模式 k 必须为 0），也可以导入 `.mdf`，直接填写材料库的完整名称，使用它的色散和吸收。命名材料支持基底、条纹和每层膜，**不需要手抄 n、k**。同一部件的材料名与数值 n/k 不能同时填写。具体操作见 [MDF 材料逐步教程](docs/MDF材料逐步教程.md)，完整示例为 [materials_fdtd_example.json](configs/materials_fdtd_example.json)。

conformal 是方角包覆近似，planarized 是填沟槽并添加顶部膜。侧壁倾斜并同时带膜等未支持组合会报错。较厚结构可能碰到固定光源或监视器平面，先用 plan 发现问题。几何字段大多以 nm 为单位，sim 中以 _um 结尾的字段则使用 μm。

<a id="step-9"></a>

## 第 9 步：运行完整的 39 点扫描

完成物理基准和默认 quick 三点后，再运行本步骤。教学五点不能替代默认门禁要求的 quick 三点。

先预览：

~~~powershell
& $lumPython run_experiments.py plan --config configs/overnight_example.json
~~~

配置共有 8 组、合计 39 点：

| 组名（--only 使用此名称） | 扫描内容 | 点数 |
|---|---|---|
| planarized_cap_nm | 平坦化膜顶厚度 0–400 nm，间隔 50 nm | 9 |
| conformal_thickness_nm | 方角包覆厚度 0–200 nm，间隔 50 nm | 5 |
| mesh_dx_nm | X 网格 70、50、35、25 nm，Y 固定 30 nm | 4 |
| mesh_accuracy_auto | 自动网格等级 2、3、4 | 3 |
| ridge_height_nm | 裸光栅高度 100、200、300、400 nm | 4 |
| ridge_cd_nm | 裸光栅线宽 1200–2000 nm，间隔 200 nm | 5 |
| ridge_shift_nm | 整体位移 −100、−50、0、50、100 nm | 5 |
| asymmetric_top_inset_nm | 裸光栅左侧顶部内缩 0、30、60、90 nm | 4 |

先执行准入检查：

~~~powershell
& $lumPython tools/run_validated_overnight.py
~~~

应显示 **OVERNIGHT GATE: PASS**。该命令读取当前真实物理基准和 quick 结果，检查完整性、源码是否匹配以及基本质量，**此时还没有启动通宵计算**。FAIL 时根据输出补齐或修复前面的基准和 quick 实验。

通过后启动：

~~~powershell
& $lumPython tools/run_validated_overnight.py --execute
~~~

它会再次检查门禁，然后顺序处理需要执行的案例，最后生成报告。完整有效的已完成案例自动跳过，旧失败项和未开始项会分批处理。接通电源，保持电脑唤醒和许可证可用，队列结束前保留命令窗口。同一输出目录有队列锁，请等待队列结束再运行该目录的其他命令。

全新计算时，39 个 case 对应约 **156 次 FDTD 求解**。用首个案例耗时估计总时间；原电脑已经全部完成时，再执行会复用有效结果。

完成后打开：

~~~powershell
Invoke-Item .\output_ah53_overnight\report.html
~~~

如果只运行其中一组，例如线宽组，可在前述基准通过后使用：

~~~powershell
& $lumPython run_experiments.py run --config configs/overnight_example.json --only ridge_cd_nm
~~~

这只选择该组调度，输出目录仍属于同一个 39 点配置，最终汇总和自动报告仍可包含其他组的状态。

<a id="troubleshooting"></a>

## 中断、续跑与常见问题

### 计算中途要停下来

在运行队列的 PowerShell 窗口按 **Ctrl+C**，等待程序记录中断并清理它自己启动的进程。提示符回来后再开始新命令。直接关闭窗口或断电可能留下不完整任务。

恢复时，先解决报错原因，再运行下面两条。例如 quick：

~~~powershell
& $lumPython run_experiments.py run --config configs/quick_check.json --retry-failed
& $lumPython run_experiments.py run --config configs/quick_check.json
~~~

第一条处理失败、中断或结果不完整的旧任务，第二条处理尚未开始的任务。**普通 run 会跳过已记录为 FAILED 的旧任务；--retry-failed 不负责从未开始的点。** 成功且结果有效的点会保留，重试前旧现场归档到该 case 的 attempts 子目录。其他实验把配置名替换为对应文件即可。

默认 39 点队列也可以重新执行 tools/run_validated_overnight.py --execute，让它按门禁和状态安排续跑。

### 遇到问题先找哪个文件

打开输出目录的 **status_overview.csv**，找到失败 case 名称和错误；再进入同名子目录，用记事本打开 **worker.log**，通常最后几行包含异常。status.json 还会记录是否超时、返回码和耗时。

| 现象 | 检查或处理方法 |
|---|---|
| 找不到 run_experiments.py | 回到第 1 步，确认当前目录；新窗口中也要确认位置 |
| $lumPython 为空或调用符号后报错 | 新窗口尚未定义变量，重新执行第 2 步 |
| 弹出 Microsoft Store 或裸 python 不可用 | 使用本教程的 & $lumPython |
| 找不到 NumPy / Matplotlib | 检查是否选中第 2 步的随附解释器；已验证的本机环境不需要重装 |
| 找不到 lumapi 或无法加载接口 | 检查当前 JSON 的 lumerical_api_python，确认是包含 lumapi.py 的目录 |
| 许可证错误、CAD 无法启动 | 查看 worker.log 的实际报错，手动确认 Lumerical 与许可证能正常求解 |
| 没有窗口但有 START | 默认隐藏运行，首次诊断可加 --visible |
| 全部 SKIP ... DONE | 已有完整有效结果；看现有报告，或为新实验设置独立配置和输出目录 |
| SKIP ... FAILED | 普通 run 不重试旧失败项，解决原因后加 --retry-failed |
| PHYSICS VALIDATION: FAIL / OVERNIGHT GATE: FAIL | 按输出处理缺失结果或检查失败，先完成第 6 步，不要修改 PASS 标签 |
| case timeout | 超过 max_case_minutes，先区分许可等待、卡住和实际计算慢，再决定是否提高上限 |
| JSON 解析错误 | 检查英文引号、逗号、括号、路径转义、是否误加注释，以及是否保存为 UTF-8 无 BOM |
| Geometry top overlaps ... | 结构过高，与固定参考面冲突；回到参数和几何预览检查 |
| 输出目录被占用 / 队列锁错误 | 等待原队列退出，避免在同一目录启动第二条队列；不要删除正在使用的锁 |
| HTML 图片缺失 | 保留整个输出文件夹，报告依赖 figures 和其他相对路径 |
| background index / addpower 弃用警告 | 当前代码已改用 index / adddftmonitor；若仍出现，检查是否运行旧副本或旧脚本。2023 R2.1 行为未实测 |

### 快捷入口分别做什么

| 入口 | 实际执行 |
|---|---|
| quick_check.bat | 计划 + 最多一个待运行 case（可见窗口）+ 报告，**不会自动完成三个点** |
| run_overnight.bat | 门禁通过后运行默认 39 点队列 |
| launch_local.ps1 -Task physics | 检查已有物理结果，**不会启动四个基准求解** |

快捷启动器在 logs 中保存总日志。初学者建议在已有 PowerShell 窗口输入 **.\quick_check.bat**，这样结束后仍可看见输出。上面的 Python 命令不依赖 PowerShell 脚本执行策略。

<a id="advanced"></a>

## 进阶功能、精度范围与更多说明

### 什么时候使用 RCWA

先完成前面的 FDTD 流程。如果需要 RCWA 反射/透射系数及 FDTD 对照，再阅读 [RCWA 后端与验收](docs/RCWA_后端与验收.md)。它是独立命令流程，不是给 run_experiments.py 加一个后端选项。

旧常数材料的 RCWA 流程包括 6 个主要控制、4 个独立弱散射原点控制、相位验收和统一系数导出。`.mdf` 命名材料使用独立的材料验收流程，包括实际 n/k 回读、复 Fresnel 平面对照；衍射结构还需要双网格、位移和原点控制。吸收基底额外实际计算 D 与 2D 厚度，检查反射收敛。证据必须与当前程序匹配且验收通过，才能导出绝对相位系数；支持范围受已验收材料、网格与对齐矩形几何约束。

FDTD 的 result.npz 中，r_E 形状为 **(衍射阶数量, 2, 2)**，矩阵行为输出 [s,p]、列为输入 [s,p]：

~~~text
          s 入射    p 入射
s 出射     rss       rsp
p 出射     rps       rpp
~~~

RCWA 还提供 t_E 和功率归一化 S11/S21，只含上侧零阶 s/p 入射两列，不是完整双侧多阶 S 矩阵。跨后端的真实验证目前覆盖反射，未验证 FDTD 透射复幅值。

### “能运行”与“达到需要的精度”分别怎么看

2026-10-23 的本机验收完成了 116 项离线测试、quick 3 点、overnight 39 点、RCWA 10 个控制及细网格 FDTD 对照。但默认 **dx=50 nm / dy=30 nm** 适合先走通流程，不能当作已达到所有研究目的的精度。

| 裸光栅对照 | 与 RCWA 的复矩阵 L2 差 | 所查最大相位差 | 预定 3% / 2° 门限 |
|---|---|---|---|
| FDTD dx=50、dy=30 nm | 约 5.97% | 约 7.74° | 未通过 |
| FDTD dx=25、dy=15 nm | 约 1.60% | 约 1.96° | 通过 |

本表的复矩阵 L2 比较取 |m|≤5，相位检查取幅值足够的 m=0、±1、±5 通道。完整 49 个共同反射阶的总功率另作检查，不能把表中 1.60% 理解为全部阶次各自的误差上界。

这些是此前常数材料控制结构的结果，不是全部 39 点或新实验的误差上界。改变波长、材料、几何或网格后，需要针对新的条件检查物理结果与收敛。上述历史验收范围为 **643 nm、二维周期、正入射、恒定实折射率**。材料扩展的使用步骤和独立检查见 [MDF 材料逐步教程](docs/MDF材料逐步教程.md)。

现已支持常见各向同性、无增益体材料的损耗与色散，一个案例一个可修改波长。现有外部 AH53 .fsp 导入适配、各向异性/旋转晶体、完整 3D LER、有限光斑与 SMASH 模块尚未完成。当前模型由 JSON 自动生成，厚基底近似不包含真实样品背面。

| 文件或目录 | 用途 |
|---|---|
| [configs/quick_check.json](configs/quick_check.json) | 标准三点小扫描，也是默认门禁证据之一 |
| [configs/tutorial_film_scan.json](configs/tutorial_film_scan.json) | 本教程的独立五点练习 |
| [configs/physics_validation.json](configs/physics_validation.json) | 四个物理基准 |
| [configs/overnight_example.json](configs/overnight_example.json) | 8 组、39 点扫描 |
| [configs/fdtd_refined_validation.json](configs/fdtd_refined_validation.json) | 更细网格的四个物理控制 |
| [docs/验证摘要.md](docs/验证摘要.md) | 验收数值与限制 |
| [docs/数值与相位校验.md](docs/数值与相位校验.md) | 坐标、偏振、归一化与相位约定 |
| [docs/RCWA_后端与验收.md](docs/RCWA_后端与验收.md) | RCWA 独立操作流程 |
| [docs/MDF材料逐步教程.md](docs/MDF材料逐步教程.md) | 导入 MDF、列名、选材、预检查、计算与读报告 |
| [docs/MDF材料验收记录.md](docs/MDF材料验收记录.md) | 材料扩展的真实验收结果与未完成范围 |
| [README_中文_先看.md](README_中文_先看.md) | 原电脑的详细验收证据入口 |
| ah53runner/、tools/、tests/ | 程序模块、辅助工具、离线测试 |

生成的 output_*、logs、local_validation 和本机解释器配置由 Git 忽略。数学源码采用逐字节指纹，.gitattributes 保留相关源码的原始换行；修改数学源码后须重新真实验证。
