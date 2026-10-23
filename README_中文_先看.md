# AH53 Experiment Runner v2（实验性）

## 本机实施记录（2026-10-22 起）

已找到本机 **Lumerical 2025 R2 / 8.34.4175**，用户已授权使用该版本；并未在 2023 R2.1 上验证。实际命令、首次属性错误、波长错误与真实求解记录见 [local_validation/实施记录.md](local_validation/实施记录.md)。

截至 **2026-10-23（Asia/Hong_Kong）**，FDTD 四例物理基准、小扫描和通宵扫描均已完成本机验收：

| 已完成核验 | 结果与证据 |
|---|---|
| 空气、n=1.5 Fresnel、±m 对称及刚性平移基准 | 4例、188项检查通过；[物理验收页](output_ah53_physics_validation/physics_validation.html) |
| 0/200/400 nm 平坦化覆盖膜小扫描 | 3/3完成；[小扫描报告](output_ah53_quick/report.html)，[4593项独立输出核验](local_validation/quick_output_audit.json) |
| 8组单变量通宵扫描 | 39/39完成、0失败、0待处理；[通宵报告](output_ah53_overnight/report.html)，[逐点物理核验](local_validation/overnight_physical_quality.json) |
| 通宵CSV、结果来源、固定坐标与HTML核验 | 59347项检查通过，1911行CSV与原始NPZ对应；[独立输出核验](local_validation/overnight_output_audit.json) |
| 重复性与位移 | quick与overnight三个相同结构的复Jones矩阵最大差均为0；[重复性记录](local_validation/06_repeatability_audit.json)。完整位移组104项检查通过；[位移核验](local_validation/06_full_translation_validation.json) |
| 浏览器显示 | 通宵90幅图全部加载，无横向溢出；[已人工验看的截图](output/playwright/overnight-report.png) |
| 细网格控制（dx=25、dy=15 nm） | 4/4真实完成，物理检查通过；[物理验收页](output_ah53_fdtd_refined/physics_validation.html)，[6114项输出核验](local_validation/fdtd_refined_validation_output_audit.json) |

**精度限制：** 固定裸Mark基准、dy=30 nm时，dx=50 nm相对dx=25 nm的复矩阵L2差仍为 **4.6634%**，dx=35 nm为 **1.9799%**（取\|m\|≤8的对角通道），尚不能声称达到计量网格收敛。dx=25 nm是数值对照，非解析真值；这些差异也不是39点扫描的统一误差上界。这里的通过表示预先固定的最小物理与内容核验通过，不代表任意结构、波长或后端都已校准。[网格对照CSV](output_ah53_overnight/mesh_dx_nm_convergence.csv)保留实际差异。

通宵第24点曾因Windows父PID复用被清理逻辑误判为失败；已保留现场、修复进程身份核验，真实重跑该点后继续队列，未直接改写为成功。说明与证据见[实施记录](local_validation/实施记录.md)。

RCWA 已真实重采6个平面/对称/位移控制和4个独立弱散射控制，**schema 2绝对横向相位证书通过**，统一输出 `r_E/t_E` 与功率归一化 `S11/S21`。每次导出重读十份原始证据并重算证书；原生数据不作相位改写。修复与范围见[RCWA实际验收](local_validation/09_RCWA实际验收.md)、[使用说明](docs/RCWA_后端与验收.md)。

平面、裸Mark、+100 nm位移三个结构与细网格FDTD的独立**反射对照均通过**：裸Mark的复矩阵L2差约1.5983%，所查最大相位差1.9568°，采用预定3%/2°门限。见[裸Mark对照](local_validation/backend_comparison_fine_bare0/backend_comparison.html)、[验收摘要](local_validation/final_validation_summary.json)。对应粗网格裸Mark仍差约5.97%、最大相位差7.74°，其FAIL记录保留。这不构成39个粗网格结构的统一误差界；跨后端只验证反射，未验证FDTD透射复幅值。S11/S21仅含上侧零阶s/p入射两列，完整双侧多阶S矩阵不在范围内。已有AH53工程连接按用户指示暂缓，SMASH尚未实施。

本机解释器是 `D:/Ansys Inc/v252/Lumerical/python/python.exe`。其自带 numpy 2.2.2、matplotlib 3.10.0，使用时无需重装；[requirements-v252.txt](requirements-v252.txt) 记录实际版本。下面原版 Python 安装建议不适用于本机现有随附环境。

本机的裸`python`命令命中WindowsApps空别名。可使用上述绝对解释器路径，或通过[launch_local.ps1](launch_local.ps1)选择已验证解释器。`-Task check`只做配置与几何检查；`-Task overnight-check`只检查通宵准入门禁。`run_overnight.bat`会先检查门禁，再顺序执行扫描。

新增 `configs/physics_validation.json` 和 `tools/validate_physics.py`，用于从真实结果验收空气、平面 Fresnel、±m 对称性及刚性平移。只有完成结果、输入摘要、源码指纹和文件校验全部匹配时才允许进入报告；旧/失败现场保存在各 case 的 `attempts/` 中。队列具有输出目录锁、中断清理和采样内存记录；`--retry-failed` 只处理失败/中断/不完整项，尚未开始的任务用普通 `run`。

所有源与监视器每组保持固定；监视器采用显式 643 nm 单频采样。实际激励为覆盖目标波长的宽带脉冲，避免原 1 nm 极窄频带脉冲超过仿真时长；不再依赖源频带中心自动选择监视频率。复 Jones 相位仍须以实际物理验收页和收敛证据判断。

**目标：** 对同一个 AH53 理想二维条纹光栅做多组单变量实验；自动生成模型、执行 FDTD、保存 `r_E(m)` 复Jones矩阵、网格/功率与近场，并生成可在浏览器中打开的 HTML 报告。原始目标版本为Lumerical 2023 R2.1；本次真实运行使用上述2025 R2环境，不能据此推定2023 R2.1兼容性。

## 当前真正实现了什么

- `run_experiments.py plan`: 参数展开 + 输入校验 + 39个独立案例（示例）+ 结构预览PNG，无需安装 Lumerical。
- `run_experiments.py run`: 调用 **本机** Lumerical Automation API（`lumapi`）执行 2D XY FDTD，每个case单独启动Python worker与Lumerical会话；每case分别运行：空环境 s参考、空环境 p参考、带结构 s、带结构 p（**4次真实FDTD**）。只有正常返回并通过完整性核验才标为DONE；失败或中断可能留下部分文件，须结合状态和校验判断。
- `run_experiments.py report`: 汇集成功case的复数数据、绘制曲线与统一色标近场、生成CSV与HTML。
- 失败日志、每个算例独立几何文件、超时清理、仅跳过已完成且结果完整的case、`--retry-failed`。
- X为母周期、Y为厚度、Z为无限长条纹；正入射 −Y。二维 s 对应 Ez，p 对应 Ex/Ey。

## 与上一份简单 ZIP 的差别

上一份膜厚脚本需要你自己先做 `film_stack` Structure Group，并**没有**完成Jones矩阵或成套报告。本版不依赖那份工程和Structure Group：通过Python配置重新生成每一个FDTD几何，计算和结果目录彼此独立。

**重要限制：** 主扫描使用FDTD，RCWA相位和跨后端反射控制的已验范围如上。任意多波长材料色散、完整3D LER、真实SMASH WQ和GUI尚未实现。材料使用恒定实折射率，程序遇到 `k!=0` 会拒绝执行。`r_sp,r_ps` 对当前各向同性二维非锥形结构按物理解耦设为0，CSV来源列明确标为对称性推导。**2023 R2.1兼容性、全部扫描的计量误差界与通用多端口S矩阵仍未验证**。

## 1. Windows 本机准备

1. 使用 **已安装并获得许可证的 Lumerical 2023 R2.1**。确保手动运行过一个小FDTD案例。
2. 安装可调用 Lumerical `lumapi.py` 的Python解释器，并执行 `python -m pip install -r requirements.txt`。所需Python包：NumPy、Matplotlib（依赖版本根据本机Python/Lumerical兼容性选取；不要安装一个未知兼容性的最新版）。
3. 在 Lumerical 安装目录搜索 `lumapi.py`；通常位于类似 `.../Lumerical/api/python` 的目录中。将包含 **lumapi.py** 的文件夹完整填写到两个配置 JSON 的 `lumerical_api_python` 字段。示例：`"C:/Program Files/Ansys Inc/v232/Lumerical/api/python"`，**仅为路径形式示例，不保证你的真实安装目录或版本号如此**。
4. 关闭自动休眠、接通电源、保证足够磁盘空间和许可证，先运行三点测试再安排通宵队列。

## 2. 第一次离线检查（不会运行Lumerical）

在解压后的目录打开 PowerShell 或CMD：

```bat
python -m unittest discover -s tests -v
python run_experiments.py plan --config configs/quick_check.json
```

输出中列出3个独立的平坦化覆盖膜测试点 `0,200,400 nm`。请用输出目录中的 `geometry_preview.png` 检查厚度/填充关系。**平坦化 `0 nm` 仍填满沟槽，不是裸Mark**。裸Mark需要 `films: []`。

## 3. 首次真实求解：仅执行1个case

```bat
python run_experiments.py run --config configs/quick_check.json --limit 1 --visible
```

这会启动一个独立worker并打开可见的 Lumerical 会话（方便定位本机版本的属性兼容问题）。如果失败，查看该case的 `worker.log`。它会将问题明确记录为FAILED，**不会伪造数据**。修改代码或配置后先重复一例验证，再运行完整任务。

首次建议的物理校验：

- 用无光栅的平面界面单独验证复 Fresnel 系数的 s/p 幅值、相位与波矢方向；
- 在有光栅的对称结构上验证 ±m 反射率对称、Jones交叉项为零；
- 验证 `sum(eta_s)` / `sum(eta_p)` 与R监视器，以及 `R+T` 的能量闭合；
- 移动整个AH53结构100 nm： ±1 相对相位变化应为4.5°，±5应为22.5°（符号取决于约定）；
- 逐步细化网格，确保反射幅度及相位收敛。

报告的质量警报是**提醒**，不是替你做了相位校准。

## 4. 执行整个小测试

```bat
python run_experiments.py run --config configs/quick_check.json
python run_experiments.py report --config configs/quick_check.json
```

打开输出目录的 `report.html`。每个case有自己的 `AH53_model.fsp`、`geometry.json`、`worker.log`、`status.json`，成功时还会有 `result.npz` 和 `physics_metadata.json`。`result.npz` 的 `r_E` 形状是 `(N_orders, 2, 2)`，矩阵行是输出 `[s,p]`，列是输入 `[s,p]`。

## 5. 大批任务（先确认小批成功）

预览39个案例：

```bat
python run_experiments.py plan --config configs/overnight_example.json
```

只运行某一组：

```bat
python run_experiments.py run --config configs/overnight_example.json --only planarized_cap_nm --limit 3
```

全部按顺序执行（推荐用批处理日志重定向；可运行压缩包内 `run_overnight.bat`）：

```bat
python run_experiments.py run --config configs/overnight_example.json
```

若夜间某例失败，记录并继续。第二天只重试失败case：

```bat
python run_experiments.py run --config configs/overnight_example.json --retry-failed
```

结束后执行：

```bat
python run_experiments.py report --config configs/overnight_example.json
```

注意，每个case实际需4次FDTD仿真；39case约为156次FDTD调用。**不应在未经小样估计运行开销与验证数值设置前直接提交39case。** 对于相同结构的参考计算，后续可以优化缓存；本版为了可靠隔离，每个case重新参考计算。

## 6. 实验扫描如何配置

编辑 `configs/overnight_example.json` 中 `experiments`。每组是独立控制变量，相对于同一 `base_model`，组级 `overrides` 可以指定不同固定基准。

```json
{
  "name": "film_scan",
  "parameter": "films.0.thickness_nm",
  "start": 0, "stop": 400, "count": 9,
  "overrides": {"films": [{"mode":"planarized","thickness_nm":0,"n":1.46,"k":0}]}
}
```

也可以写 `"values": [0,50,...]` 或 `"start":0,"stop":400,"step":50`。数组子项路径通过点号与索引指定，例如 `films.0.n`。

**网格扫描：**

- `mesh.mode = "override"`: 扫描 `mesh.dx_nm`,`mesh.dy_nm`；固定Mesh Override生效。
- `mesh.mode = "auto"`: 扫描 `mesh.mesh_accuracy`；程序拒绝在Override打开时把Mesh Accuracy变化伪装成网格变化。

**几何扫描：** `ridge_cd_nm`,`ridge_height_nm`,`ridge_shift_nm`,`ridge_center_offsets_nm`（整体数组更改目前建议在组级overrides中处理）, `ridge_top_left_inset_nm`,`ridge_top_right_inset_nm`。侧壁非垂直且有覆盖膜时程序明确拒绝，不会用假的几何继续运行。

**覆盖膜：** `films` 可以有多层，每层 `mode: none/conformal/planarized`, `thickness_nm`, `n`, `k=0`。`conformal` 是具有方角的直角近似包覆，**不是**工艺真实等厚曲面。`planarized` 是填充沟槽并在前一最高表面加顶层厚度，和沉积/刻蚀动力学无关。

## 7. 报告与各阶复系数

结果输出（完整有效结果须同时通过status、输入摘要、源码指纹及文件校验；失败现场也可能保留部分同名文件）：

```text
output_ah53_overnight/
  manifest.json
  report.html
  coefficients_long.csv         # rss/rsp/rps/rpp复数实虚部/模/相位 + R_m
  status_overview.csv
  quality_warnings.csv
  *_convergence.csv             # 网格组：相对复数矩阵收敛性
  *_translation_validation.csv  # 位移组：相位变化定律
  figures/
    *_rss_amplitude.png
    *_rpp_amplitude.png
    *_plus_minus_phase.png
    *_reflected_power.png
    *_complex_plane.png
    field_*.png
  <case_id>/
    case.json                   # 配置冻结 + fingerprint
    geometry_preview.png        # 概念几何，非物理求解结果
    geometry.json
    AH53_model.fsp
    result.npz                  # 求解得到的复矩阵；文件存在不等于case成功
    physics_metadata.json
    nearfield_s.npz / nearfield_p.npz    # 部分代表case
    worker.log
    status.json
    raw_*.npz                   # 四阶段原始监视器E/H、坐标、频率与通量
    engine_logs/                # 各阶段真实引擎日志
    attempts/                   # 重试前的失败/旧接口现场
```

复矩阵s/p相位定义与参考面请看 `docs/数值与相位校验.md`。零振幅相位不应该解读为有意义的角度。所有级次保存，图形默认强调 `m=0,1,5`，便于与AH53内部子条纹的 `m=5` 对照；修改`report.py`可添加其他阶次。

报告使用各组、各偏振共同的近场色标，显示`|E|²/|E_inc|²`；相位低于绝对/相对幅值门限时留空。`--only`用于report时会覆盖该输出目录的报告为指定组范围；完整报告请省略`--only`。manifest始终保存配置的全部case，旧图和失败文件保留但不混入当前有效报告。

## 8. 夜间无人值守的安全限制

- 顺序求解而非同时启动多个FDTD求解器，避免争抢内存与许可证。
- **最长允许单case运行时间**由 `max_case_minutes` 控制。超时后尝试杀掉该worker进程树；少数情况下许可证服务或外部进程可能需要人工确认。
- 不要将脚本目录放在同步冲突的网络盘。充分确保磁盘可用，使用本地路径。
- Windows锁屏通常可以继续运行，但**休眠、关机、注销会中断任务**；还要遵守公司资源使用与安全规定。
- 默认无计划定时调度：用户手动启动 `run_overnight.bat` 后，它会在本机连续计算；ChatGPT不会在后台替用户计算。
- 输出没有真实误差条、没有设备WQ，也不代表已经计算了非周期有限Mark的真实光斑扫描；当前为无限周期、正入射、实n的教学基准。

## 9. 常见失败与排查

| 现象 | 优先检查 |
|---|---|
| `Cannot import lumapi` | 配置 `lumerical_api_python` 是否指向真正有 `lumapi.py` 的目录 |
| `LumApiError` 属性找不到 | 对照本机2023 R2.1对象属性，发回worker.log修正Python API属性 |
| `No license` | Lumerical实际许可证与本机计算资源 |
| `source`/监视器重叠膜层 | 减小膜厚、调整计算区域与所有固定监视面；不要自动把相位参考面跟着膜层移动 |
| 物理能量不守恒 | 参考场、FFT投影、监视器平面、网格与PML，严格检查R/T来源 |
| 位移相位变化错误 | 相位参考面、时间因子、傅里叶符号、p基矢，优先排查而非调参使之“碰巧接近” |
| 修改`mesh accuracy`图却几乎不变 | 检查是否仍启用Mesh Override，以及实际网格密度是否改变 |

---

**开发状态说明：** 本机2025 R2的FDTD物理控制、quick三点、overnight39点、RCWA十个控制和细网格反射交叉验收已完成，失败与修复记录均保留。最终[116项离线检查通过](local_validation/10_rcwa_absolute_origin_offline_tests.log)。2023 R2.1兼容性、全部粗网格扫描的精密收敛、原工程适配和SMASH仍需后续工作。
