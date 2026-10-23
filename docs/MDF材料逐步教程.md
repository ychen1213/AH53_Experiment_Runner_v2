# 使用自己的 MDF 材料库：一步一步操作

本功能让基底、条纹和每层膜直接使用 **Lumerical 材料库中的完整材料名**。不需要把各个波长的 n、k 抄到 JSON 中。程序导入 `.mdf`，按本案例的波长读取材料，并记录求解实际采用的拟合 n、k。

FDTD 和 RCWA 都支持这套选材方式。目前支持二维、正入射、各向同性、无增益的体材料。一个案例计算一个波长；修改 `wavelength_nm` 可以换波长。各向异性、旋转晶体、二维表面材料等尚不在此范围内。

这里的命令全部输入 **PowerShell**，不要输入 Lumerical 的 Script Prompt。JSON 是参数文件，语法与 LSF 不同；JSON 中不能写 `addrect;`、`set(...)` 或注释。

## 1. 打开项目并选择 Python

原电脑执行：

```powershell
Set-Location -LiteralPath 'D:\工作\AH53_Experiment_Runner_v2'
$lumPython = 'D:\Ansys Inc\v252\Lumerical\python\python.exe'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
```

换电脑时修改项目和 Python 路径，并修改所用 JSON 中的 `lumerical_api_python`，使它指向包含 `lumapi.py` 的文件夹。详细环境设置见 [README 第 1～3 步](../README.md#step-1)。已实际使用的版本为 **2025 R2**，2023 R2.1 尚未实测。

## 2. 暂时没有 MDF 时，先生成教学材料库

程序不会随 GitHub 分发你的材料数据。可以从本机安装的 Lumerical 内置材料中创建一份演示副本：

```powershell
& $lumPython tools/materials.py export-demo --api-path 'D:/Ansys Inc/v252/Lumerical/api/python' --output materials/ah53_demo.mdf
```

成功后出现 `EXPORTED LOCAL DEMO`，并生成：

- `materials/ah53_demo.mdf`：用于下面的演示配置。
- `materials/ah53_demo.manifest.json`：记录来源、拟合设置和文件哈希。

如果提示文件已存在，直接继续下一步即可。此命令拒绝覆盖已有材料库。它只修改临时会话里的材料副本，不修改 Lumerical 安装库。

演示名称如下。它们用于功能验收，不代表你的样品或实测材料。

| 演示材料完整名 | 来源 |
|---|---|
| `AH53_TEST_Silicon` | `Si (Silicon) - Palik` |
| `AH53_TEST_Silica` | `SiO2 (Glass) - Palik` |
| `AH53_TEST_Gold` | `Au (Gold) - CRC` |

## 3. 读取材料库的真实名称

演示库执行：

```powershell
& $lumPython tools/materials.py list --config configs/materials_fdtd_example.json --filter AH53_TEST
```

这里会打开隐藏的 Lumerical 会话、导入材料、列出名称，然后关闭；不会运行电磁求解。删除 `--filter AH53_TEST` 可以查看会话中的全部材料，包括安装时自带的材料。

以后拿到自己的文件，例如 `D:\材料\my_materials.mdf`，可先独立查看：

```powershell
& $lumPython tools/materials.py list --api-path 'D:/Ansys Inc/v252/Lumerical/api/python' --database 'D:/材料/my_materials.mdf'
```

**复制输出中的完整名称，包括空格、大小写、括号、后缀。** 文件名与材料名是不同的概念：一个 `.mdf` 可以包含很多个材料，JSON 要同时填写文件路径和具体材料名称。

## 4. 理解材料字段应该放在哪里

打开完整示例：

```powershell
notepad .\configs\materials_fdtd_example.json
```

下面只是选材部分的示意，不能直接替换整个配置文件：

```json
{
  "material_databases": ["../materials/ah53_demo.mdf"],
  "base_model": {
    "wavelength_nm": 643,
    "substrate_material": "AH53_TEST_Silicon",
    "ridge_material": "AH53_TEST_Silicon",
    "films": [
      {
        "mode": "planarized",
        "thickness_nm": 100,
        "material": "AH53_TEST_Silica"
      }
    ]
  }
}
```

| 字段 | 填什么 | 对应位置 |
|---|---|---|
| 最外层 `material_databases` | 一个或多个 `.mdf` 路径 | 导入的材料库文件 |
| `base_model.substrate_material` | 完整材料名 | 基底 |
| `base_model.ridge_material` | 完整材料名 | 光栅条纹 |
| `base_model.films` 内的 `material` | 完整材料名 | 对应的一层膜 |
| `base_model.wavelength_nm` | 正数，例如 `643` | 此案例计算波长，单位 nm |

相对路径以 **JSON 所在文件夹** 为起点。配置在 `configs` 中，因此 `../materials/ah53_demo.mdf` 表示先退到项目根目录，再进入 `materials`。也可以使用 `D:/材料/my_materials.mdf` 这样的绝对路径。

同一个部件只能选一种方式：

- 使用材料名时，删除该部件的数值 `n`、`k` 字段。比如填写 `substrate_material` 后删除 `n_substrate` 和 `k_substrate`。
- 保留旧的常数折射率方式时，继续填写 `n_substrate`、`n_ridge`，膜填写 `n` 和 `k: 0`，不添加该部件的材料名。
- 不同部件可以混用两种方式，例如基底用库材料、膜用恒定实数 n。

多层膜就在 `films` 数组中逐层增加对象，各层可用不同材料。`planarized` 的 0 nm 仍可能填充沟槽；它不是“删除这层膜”。完全去掉膜请使用 `"films": []`。几何定义见 [README](../README.md#step-8)。

## 5. 开始求解前，检查材料

```powershell
& $lumPython tools/materials.py check --config configs/materials_fdtd_example.json --output local_validation/my_material_check.json
```

期望看到 `PASS`，以及各角色的材料名、n、k。此步骤仍不进行电磁求解。

检查包括：材料存在、数据覆盖波段、各向同性、无增益、实际拟合曲线与原始材料数据误差，以及 MDF 是否改变。同材料、同波长的多个膜厚会共用一次材料检查；不同波长会分别检查。

默认光源和材料拟合范围为目标波长 ±100 nm。例如目标 643 nm 对应 543～743 nm。若材料数据范围较窄，可在 `base_model.sim` 中显式设置 `"source_bandwidth_nm": 100`，表示总宽度 100 nm，也就是 593～693 nm。必须在这个完整范围内有材料数据，不能只保证中心波长有数据。

**报告中的 `fit_n / fit_k` 是求解采用的拟合值；`raw_n / raw_k` 是库数据响应。** 两者通常接近，但不保证相等。FDTD 需要可在时域计算的色散模型，RCWA 配置为采用相同拟合方式和波段，以便比较。预检查在拟合波段内取 1001 个采样点，并检查目标波长；这些采样点的最大相对复介电常数拟合误差默认限为 5%，这不是对连续频谱每一点的数学证明。检查失败时应检查材料数据和材料拟合设置，不能把原始 n、k 当成实际求解值。

## 6. 预览教学扫描

```powershell
& $lumPython run_experiments.py plan --config configs/materials_fdtd_example.json
```

这份示例使用硅基底、硅条纹和二氧化硅平坦化膜，扫描膜顶覆盖厚度 **0、100、200 nm**。在 `output_ah53_materials_example` 中查看几何预览。预览显示材料名称，实际 n、k 要由 Lumerical 读取，不会离线猜测。

## 7. 先算一个点，再算完整三点

```powershell
& $lumPython run_experiments.py run --config configs/materials_fdtd_example.json --limit 1
```

完成后再运行：

```powershell
& $lumPython run_experiments.py run --config configs/materials_fdtd_example.json
```

完整且与当前输入一致的已完成案例会跳过。计算失败的案例保留日志；排除原因后使用：

```powershell
& $lumPython run_experiments.py run --config configs/materials_fdtd_example.json --retry-failed
```

`--retry-failed` 只重试失败案例，不会启动仍未执行的点。要继续剩余队列，使用上一条不带这个选项的命令。

## 8. 看结果及实际采用的材料

```powershell
& $lumPython run_experiments.py report --config configs/materials_fdtd_example.json
Start-Process .\output_ah53_materials_example\report.html
```

除了原有反射强度和相位图，还可读取：

| 文件 | 用途 |
|---|---|
| `material_summary.csv` | 用 Excel 查看每个案例、每个部件的材料名、拟合 n/k、原始 n/k、波长、拟合范围、MDF 哈希 |
| `power_summary.csv` | 查看两种偏振的 R、内部监视面 T、`1-R-T` 残差及监视面位置 |
| 每个案例的 `material_resolution.json` | 查看完整材料解析与拟合检查记录 |
| 每个案例的 `physics_metadata.json` | 查看真实求解版本、材料与计算证据 |
| 每个案例的 `.fsp` | 在 Lumerical 中查看实际建立的模型 |

吸收材料允许 `R+T<1`。FDTD 的 T 是指定内部监视面上的通量，示例位置为基底内 `y=-1.05 μm`。`1-R-T` 是两个监视面之间的通量残差，不能单靠这个代数关系证明吸收计算正确，也不能当成整块有限厚样品的吸收率。本程序仍采用厚基底近似，没有加入真实样品的背面。

原始无损材料可能拟合出极小的正 k。程序保留实际值；只有原始 k 为 0 且拟合 k 不超过 `1e-8` 时，才归为可忽略拟合损耗，并仍执行严格的近似无损通量检查。这个上限在 643 nm、1.05 μm 深度对应约 `2.1e-7` 的强度衰减。原始材料只要具有正 k，即使很小，也按吸收材料处理。

## 9. 换成自己的材料库和材料

建议复制配置，保留可复用的教学示例：

```powershell
Copy-Item .\configs\materials_fdtd_example.json .\configs\my_material_scan.json
notepad .\configs\my_material_scan.json
```

依次修改：

1. `output_dir` 改为新目录，例如 `../output_my_material_scan`。
2. 最外层 `material_databases` 改为真实 MDF 路径。
3. 基底、条纹、各层膜的名称改为第 3 步列出的完整名称。
4. `wavelength_nm` 改为你的波长。
5. `experiments` 中的 `values` 改为要扫描的厚度列表。

然后把第 5～8 步命令中的配置名替换为 `configs/my_material_scan.json`，先检查，再预览，再算一个点。

MDF 内容改变后，旧结果不能继续冒充新材料的结果。程序将材料库 SHA256 纳入冻结输入；改变材料库后需要重新计划/计算，历史文件会保留。名称相同但内容不同的多库覆盖会被拒绝；建议给实验材料使用明确、唯一的名称。

项目的 `.gitignore` 默认排除 `.mdf` 和 `materials` 下的本机文件。你的实际库放在本机即可，不需要上传到公开 GitHub。

## 10. 材料物理检查与 RCWA

材料数据检查通过后，还需要真正运行物理基准。示例平面配置分别覆盖二氧化硅、吸收硅、金，以及另一个波长的硅：

```powershell
& $lumPython run_experiments.py run --config configs/materials_fdtd_validation.json
& $lumPython tools/validate_materials.py --config configs/materials_fdtd_validation.json
```

它把真实 FDTD 反射复幅值与复 Fresnel 公式比较，并独立检查基底内部 T 的解析衰减，报告写入该配置的输出目录。这个验收只覆盖实际运行的材料、波长和网格，不自动证明任意新材料或光栅精度。

RCWA 使用相同材料字段，命令和材料专用验收见 [RCWA 后端与验收](RCWA_后端与验收.md)。吸收基底在 RCWA 中需要显式厚吸收层和无损终止介质，程序自动计算厚度，并实际比较 D 与 2D 两次反射结果。其终止面透射与 FDTD 内部监视面 T 定义不同，不能直接相减比较。

## 常见问题

| 提示或现象 | 处理方法 |
|---|---|
| 找不到 MDF | 检查路径；相对路径相对于 JSON 文件，不是 PowerShell 当前目录 |
| 材料不存在 | 执行 `list`，复制完整名称；检查是否导入了正确文件 |
| 名称与数值 n/k 冲突 | 删除同一部件的旧数值字段，或改回数值模式 |
| 数据范围不足 | 检查材料覆盖的波长和 `source_bandwidth_nm`，不要外推使用 |
| 拟合误差、负吸收或各向异性检查失败 | 在 Lumerical 材料编辑器中检查材料类型、采样数据与拟合；修正后重新导出 MDF |
| 修改 MDF 后旧结果不能用 | 重新运行计划和计算，历史结果会保留 |
| 更换波长后 n/k 不同 | 色散材料的正常行为，查看对应案例的材料表 |
| `list` 找到了材料，但 `check` 不通过 | 列名只说明材料存在，不代表类型、波段或拟合已满足支持条件 |

官方接口参考：[导入 MDF](https://optics.ansys.com/hc/en-us/articles/360038629493-importmaterialdb-Script-command)、[导出 MDF](https://optics.ansys.com/hc/en-us/articles/360038629273-exportmaterialdb-Script-command)、[FDTD 拟合折射率](https://optics.ansys.com/hc/en-us/articles/360034409694-getfdtdindex-Script-command)。
