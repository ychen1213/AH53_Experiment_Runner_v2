# 隔离 RCWA 后端

本阶段仅支持空气上方正入射、无损且各向同性的恒定实折射率、平面界面、矩形 AH53 和共享几何生成器已有的矩形膜层。斜壁、损耗、各向异性、斜入射、越过周期边界的条纹和不同参考面明确拒绝。材料数值与几何均来自本工程 `build_geometry`；旧 `RCWA_Lumerical_Benchmarks` 的 n=1.45 数值不作为本工程 n=1.5 的验证结果。

`ah53runner/rcwa.py` 独立于 FDTD solver、runner、artifacts、report。坐标作正旋转 `(x,y,z) -> (x,z,-y)`，行列式为 +1，使用本机已验证的 z 轴 Forward、`2D Y-normal`。RCWA 外界面固定在旋转后 z=-1800 nm（入射/反射）和 z=+1050 nm（透射），与 AH53 y=1.80/-1.05 μm 对应；膜厚变化不移动参考面。

## 可先执行的离线操作

PowerShell 中使用本机已核验的解释器：

```powershell
$lumPython = 'D:\Ansys Inc\v252\Lumerical\python\python.exe'
& $lumPython tools/run_rcwa.py --help
& $lumPython tools/run_rcwa.py plan --config configs/rcwa_validation.json
& $lumPython -m unittest discover -s tests -p test_rcwa.py -v
```

默认命令是 `plan`。计划只生成冻结输入与 LSF，不启动 CAD 或求解。默认计划为 n=1.5 平面、裸 AH53 零位移、裸 AH53 +100 nm，分别采用粗细两个网格，共 6 次原始采集。每例初始超时为 15 分钟。

## 顺序真实采集

仅在 FDTD 物理基准与 quick 三点均通过、且没有正在运行的 FDTD worker 后，才可执行：

```powershell
& $lumPython tools/run_rcwa.py run --only rcwa_planar_n15 --level coarse --limit 1
& $lumPython tools/run_rcwa.py run
& $lumPython tools/run_rcwa.py run --retry-failed
& $lumPython tools/run_rcwa.py run --config configs/rcwa_origin_validation.json
```

每次原始采集使用独立 worker 与一个隐藏 CAD 会话，记录自己的进程身份，顺序运行并关闭；不改全局资源设置。已完成且证据完整的原始采集跳过；失败现场留存，重试归档到当前目录的 `attempts/`。若关闭或超时清理失败，停止队列以免重叠。实际 `interface actual positions`、`actual mesh cells x`、`num_k` 必须与冻结接口、网格及原生阶轴一致；属性不支持时保留真实异常，标记失败后修复，不能冒充成功。

完整原始状态为 `RAW_COMPLETE_PHASE_UNCALIBRATED`，只表示原始证据完整。每例保留 `input.json`、`build_run_export.lsf`、`worker.log`、`rcwa_model.fsp`、Lumerical 直接导出的 `native_rcwa.mat`、全部 native 数据集的 NPZ/JSON、实际版本、求解设置和文件 SHA256。此时不创建统一系数文件，不宣称相位已校准。

## v252 原生 index 的保真传输

本机 2025 R2 / 8.34.4175 的 RCWA `index` 数据集使用 z 层界面坐标，但折射率属性按相邻界面之间的层区间采样。真实首个平面算例含 800 个 x 采样、1 个 y 采样、5 个 z 界面和 1 个波长，实际 `index_z` 只有 `800×1×4=3200` 个复数值。原始 MAT 中它是 MATLAB struct 内的 packed 数组；只有 `index_z` 的情况不补造 `index_x/index_y`。原生 z 坐标可以包含求解区域边缘，不应把它与设置的内部界面表逐项混同。

随附 `lumapi.py` 的 `RectilinearDatasetTranslator` 按界面点数假定 `800×1×5=4000` 个值，因此会在真实求解完成后报 reshape 错误。这是该数据集的传输形状约定问题；没有通过补值或改变材料来处理。

`tools/run_rcwa.py` 的 `read_packed_native_index` 仅在独立 worker 的这一次 index 传输中临时跳过 rectilinear 形状解释，保留真实 packed 数组、原始 `Lumerical_dataset` metadata 和数组原有尺寸。其他矩阵数据集继续使用原有翻译规则；`finally` 在成功或异常时均恢复原来的 API 方法。Lumerical 安装目录及其 `lumapi.py` 没有修改。

`native_index.npz/json` 与 Lumerical 直接保存的 `native_rcwa.mat` 均保留；`native_status.json` 的 `native_index_transfer` 记录坐标长度、z 界面/区间数、原生属性尺寸、元素数，并明确 `attribute_reshape_applied=false` 和 `interpolation_or_padding_applied=false`。元素数不符合实际层区间数量时明确失败。这里不猜测 packed 数组的空间轴顺序，不插值、不填充，也不把未验证的三维重排当成几何校验通过。

## 相位注册与收敛验收

主控制为两档网格各三个结构，共 6 例；独立绝对原点控制为两档网格各两个弱折射率，共 4 例。原点控制使用空气基底、100 nm 高度、1600 nm CD、3200 nm 子周期、折射率 1.0001/1.00005，除折射率外几何和其他模型输入完全相同。

每一网格必须有自己的真实 n=1.5 平面、对称裸 AH53、刚性 +100 nm 平移。平面验证两个同向 p 基/s 基的复 Fresnel 系数、透射复幅值与非零衍射阶；镜像/平移检查使用 ±1、±5 阶，任一必要幅值低于 1e-4 即拒绝相位判定。平面界面处两反射对角均为 -0.2；固定 1.8 μm 参考面、643 nm 时均为 `0.162717428756567+0.116288599522273j`。

镜像、刚性平移和 Fresnel 检查不能独立识别所有阶共同携带的 `(-1)^m`，所以旧 `native`/`half_cell` 相对检查通过不能声明绝对 x=0 相位。原生数据和历次 PASS/FAIL 证据均保留。

```powershell
& $lumPython tools/run_rcwa.py calibrate --registration cell_origin_half_cell `
    --origin-config configs/rcwa_origin_validation.json
```

三个固定候选为 `native`、`half_cell=exp(-i*2π*m*(dx/2)/P)`、`cell_origin_half_cell=exp(-i*2π*m*(-P/2+dx/2)/P)`。最后一项把原生 DFT 第一个样点注册到真实物理坐标 `x_j=-P/2+(j+1/2)dx`。每次读取原始采集时，完整 native index x 坐标必须实测符合此规则；不从模型猜测实际样点，也不移动数组。导出只覆盖边界对齐的矩形，非对齐几何拒绝。

校准直接读取四份完整原始采集，调用 `tools/validate_rcwa_origin.py` 的无写入纯函数重新计算 TE Born 解析复幅值：m=0、±1、±2、±5，幅值至少 1e-7，复相对误差≤1%、相位误差≤1°；每个网格的两个实际 χ=n²−1 还须满足 r/χ 复相对差≤0.5%。p 结果仅作诊断。必须唯一通过 `cell_origin_half_cell`，并与请求的注册一致，才可能产生绝对相位证书。不读取现有诊断报告的 PASS 标签，不拟合自由相位或使用 FDTD 目标拟合；失败时不能提高门限。

此外每一控制结构都比较相邻粗细网格及递增傅里叶截断：`|m|<=5` 的全部 r_E/t_E 通道复相对差≤3%，分母为 `max(|fine|,1e-4)`；全传播阶总 R/T 功率的最大绝对差≤1%。这些阈值在真实运行前固定。失败后应增加截断和采样重新采集；不声称全部高阶、所有几何或任意波段都达到此误差。

`rcwa_phase_calibration.json` 使用 schema 2，只有原有 43 项控制/收敛检查和新增绝对原点检查均通过才为 PASS，明确标记 `VERIFIED_ABSOLUTE_X0`。证书分别绑定主控制和四份 Born 控制的冻结输入、完整原始文件 SHA256、状态 SHA256、真实版本、网格/截断、全部候选逐阶结果与解析工具源码 SHA256；弱空气控制不扩大 n=1.5 的目标范围。每次导出重新核验全部 10 份证据并纯计算重建完整证书；完整 canonical 内容必须相等，没有时间戳参与数学证书。`--origin-config` 的当前冻结输入计划也必须与四份证据相同。

后端指纹包括 `rcwa.py`、共享 `geometry.py` 和 Born 验收工具源码。数学变动后必须真实重新采集 6+4 例；旧指纹不能迁移或改写为新指纹。旧 schema 1 或仅相对 half_cell 的证书不能导出绝对相位。

每次验收的完整 PASS/FAIL 副本均保留在 `calibration_attempts/`，切换注册候选或重算不会抹去前次失败。

## 统一系数导出

```powershell
& $lumPython tools/run_rcwa.py export --origin-config configs/rcwa_origin_validation.json
```

仅证书与当前真实采集一致时输出 `rcwa_coefficients.npz` 和元数据。包含：

- `r_orders/t_orders`、对应角度与波长；反射和透射传播阶分别保存，数目可能不同。
- `r_E/t_E`：行输出、列输入的电场振幅矩阵，AH53 的 s=+Z、输入 p=+X，两个输出侧的 p 均取正 X 切向分量。
- `r_power/t_power`，别名 `S11/S21`：功率归一化矩阵，乘子分别为 `sqrt(q_out/q_in)`；各输入偏振的 `eta_r_s/p`、`eta_t_s/p` 为输出偏振功率之和。

这些矩阵只含上方零阶入射的 s/p 两列；不是完整双端口、多入射阶 S 矩阵。原生倏逝阶完整保留在 native 数据中，不混入可解释的远场功率。

原生 `Rps` 是 p 入射、s 出射，矩阵排列为 `[[Rss,Rps],[Rsp,Rpp]]`。逐阶偏振转换由 Cartesian 场及实际波矢构造，独立核验 characterization 的转换值；不能只给所有 Rpp 统一乘负号。功率还与原生 `grating_orders`、`total_energy` 交叉核验。

接口定义参考官方 [RCWA Simulation Object](https://optics.ansys.com/hc/en-us/articles/12959229278611-RCWA-Solver-Simulation-Object)、[Source polarization](https://optics.ansys.com/hc/en-us/articles/4414575008787-RCWA-Solver-Introduction) 和 [RCWA resources](https://optics.ansys.com/hc/en-us/articles/15337068502675-RCWA-Resource-Configuration)。在线文档包含晚于本机 2025 R2 的选项，实际 API 与版本记录优先。
