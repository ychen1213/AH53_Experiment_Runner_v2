# AH53 Experiment Runner v2

使用 Lumerical 顺序运行 AH53 二维周期条纹的单变量实验，保存逐衍射阶复 Jones 系数、功率、相位和近场，生成 CSV 与 HTML 报告。

已在 **Windows / Lumerical 2025 R2（8.34.4175）** 上真实运行验证。原始目标版本 2023 R2.1 尚未验证；请勿据此假定两个版本的脚本 API 一致。

## 功能

- 单变量配置展开、几何截面预览、独立 worker 顺序求解、超时与中断清理、失败现场保留及选择性重试。
- FDTD 的空环境 s/p 参考与结构 s/p 四次真实求解，输出 `r_E`、功率归一化反射矩阵、衍射效率与近场。
- 输入摘要、源码指纹、文件校验和及结果完整性检查；有效结果才进入报告。
- 空气与 Fresnel、±m 对称、刚性平移相位验证，以及网格比较。
- 独立 RCWA 后端，输出 `r_E/t_E` 和功率归一化 `S11/S21`；导出前重算六个主控制与四个独立弱散射原点控制的真实证据。

## 本机验收结果

2026-10-23 完成：116 项离线测试通过；quick 三点和 overnight 39 点全部完成；RCWA 十个控制及细网格 FDTD 三个结构的反射对照通过。

**粗网格精度仍有限制。** 默认 dx=50 / dy=30 nm 的裸 Mark 与 RCWA 的复矩阵 L2 差约 5.97%、最大所查相位差约 7.74°，未通过严格对照。dx=25 / dy=15 nm 的对应控制约为 1.60% / 1.96°，通过预定 3% / 2° 门限。这些控制不构成全部 39 个粗网格结构的误差上界。

详见 [精简验收摘要](docs/验证摘要.md) 和 [RCWA 后端与验收](docs/RCWA_后端与验收.md)。仓库提供软件和验收摘要；完整仿真工程、原始数组、许可证诊断、进程信息及机器环境日志保留在原运行电脑，不随代码提交。

## 环境与本地配置

真实求解需要自行安装并获得许可的 Lumerical，本仓库不包含其软件、`lumapi.py` 或许可证。离线规划和测试只依赖 Python、NumPy 与 Matplotlib。

本次已验证的随附解释器为 Python 3.13.1、NumPy 2.2.2、Matplotlib 3.10.0；[requirements-v252.txt](requirements-v252.txt) 记录实际版本。使用已有随附环境时无需重装依赖。原 [requirements.txt](requirements.txt) 是早期环境约束，不能直接用于该 Python 3.13 随附环境。

1. 将要使用的 `configs/*.json` 中 `lumerical_api_python` 改为实际含 `lumapi.py` 的目录。配置里的 `D:/Ansys Inc/v252/...` 是已验证机器的安装位置，其他电脑必须自行修改。
2. 若使用 Windows 启动器，复制 `configs/local_runtime.example.json` 为 `configs/local_runtime.json`，设置实际 `python_executable`。该本机配置已被 Git 忽略；也可设置 `AH53_PYTHON` 环境变量。
3. 使用独立的可写输出目录，保留失败日志和原始求解文件。许可证与计算资源需在本机可用。

PowerShell 示例，先将解释器路径替换为实际路径：

```powershell
$lumPython = 'C:\Program Files\ANSYS Inc\v252\Lumerical\python\python.exe'

# 离线测试与几何规划，不启动求解器。
& $lumPython -m unittest discover -s tests -v
& $lumPython run_experiments.py plan --config configs/quick_check.json

# 首个真实案例；随后完成物理基准，再运行完整小扫描。
& $lumPython run_experiments.py run --config configs/quick_check.json --limit 1 --visible
& $lumPython run_experiments.py run --config configs/physics_validation.json
& $lumPython tools/validate_physics.py
& $lumPython run_experiments.py run --config configs/quick_check.json

# 只有当前源码与真实物理/小扫描证据通过门禁，才执行通宵队列。
& $lumPython tools/run_validated_overnight.py --execute
```

报告在相应配置指定的 `output_dir` 下生成。`output_*`、`logs`、`local_validation` 等运行产物均不进入 Git。

## 数据定义与范围

- X 为周期、Y 为厚度，正入射沿 −Y；s=+Z，输入 p=+X。
- `r_E/t_E` 行为输出 `[s,p]`、列为输入 `[s,p]`；功率归一化矩阵模平方按输出偏振求和得到效率。
- 当前 S11/S21 只含上侧零阶 s/p 入射两列，未提供完整双侧多阶 S 矩阵。
- 真实验证限于 643 nm、二维周期、正入射、恒定实折射率；二维交叉偏振项按解耦关系设零并标明来源。
- RCWA 绝对相位导出限于已验收网格与对齐矩形几何。透射通过 RCWA 内部功率和解析平面控制，尚未作 FDTD 透射复幅值交叉验证。
- 现有外部 AH53 `.fsp` 适配、材料损耗/色散、3D LER、有限光斑与 SMASH 模块不在本次已验范围内。

## 目录

| 路径 | 用途 |
|---|---|
| `ah53runner/` | 几何、FDTD/RCWA、结果核验和报告 |
| `configs/` | 小扫描、物理控制、通宵扫描与 RCWA 配置 |
| `tools/` | 验收、审计、门禁与 RCWA 命令 |
| `tests/` | 离线回归测试 |
| `docs/` | 数值约定、RCWA 验收与精简结果 |

[原本机使用说明](README_中文_先看.md) 保留详细操作和本机历史结果引用。其中指向 `local_validation/` 或 `output_*` 的链接只在保留完整运行数据的原电脑上可用；克隆仓库后需真实运行生成，不能将缺失文件当作已通过的验收证据。

数学源码采用逐字节指纹；`.gitattributes` 对参与指纹的文件保留原始换行，避免 Git 自动换行转换造成无意的指纹变化。修改数学源码后须重新真实验证。
