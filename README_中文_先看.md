# AH53 Experiment Runner v2：本机验收记录入口

**第一次使用请阅读 [README.md：一步一步使用教程](README.md)。** 教程包含打开 PowerShell、设置解释器、预览模型、首次求解、报告读法、修改参数和失败续跑；原有操作步骤已合并到该教程，避免两套指令互相冲突。

当前使用的是已安装并获用户授权的 **Lumerical 2025 R2 / 8.34.4175**，解释器为 D:/Ansys Inc/v252/Lumerical/python/python.exe。Python 3.13.1 自带 NumPy 2.2.2 和 Matplotlib 3.10.0，无需重装依赖。**2023 R2.1 兼容性尚未实际验证。**

## 本机证据链接

下面链接指向原运行电脑保留的完整数据；这些目录由 Git 忽略，**在 GitHub 上或刚下载的副本中不可用**。仓库内可直接阅读的数值说明见 [验证摘要](docs/验证摘要.md) 和 [RCWA 后端与验收](docs/RCWA_后端与验收.md)。

截至 2026-10-23（Asia/Hong_Kong）：

| 已完成项目 | 原电脑证据 |
|---|---|
| 实际命令、首次属性/波长错误与修复过程 | [实施记录](local_validation/实施记录.md) |
| 空气、n=1.5 Fresnel、±m 对称及刚性平移，4 例、188 项检查通过 | [物理验收页](output_ah53_physics_validation/physics_validation.html) |
| 平坦化覆盖膜 0/200/400 nm，3/3 完成 | [小扫描报告](output_ah53_quick/report.html)、[4593 项输出核验](local_validation/quick_output_audit.json) |
| 8 组单变量扫描，39/39 完成 | [通宵报告](output_ah53_overnight/report.html)、[逐点物理核验](local_validation/overnight_physical_quality.json) |
| 通宵 CSV 与原始数组、来源、坐标和报告，59347 项检查通过 | [独立输出核验](local_validation/overnight_output_audit.json) |
| quick 与 overnight 三个相同模型的复矩阵最大差为 0 | [重复性记录](local_validation/06_repeatability_audit.json) |
| 完整位移组，104 项检查通过 | [位移核验](local_validation/06_full_translation_validation.json) |
| 通宵报告 90 幅图加载正常，无横向溢出 | [浏览器截图](output/playwright/overnight-report.png) |
| dx=25、dy=15 nm 的 4 个控制完成，物理检查通过 | [细网格物理验收](output_ah53_fdtd_refined/physics_validation.html)、[6114 项输出核验](local_validation/fdtd_refined_validation_output_audit.json) |
| RCWA 重采 6 个主要控制及 4 个弱散射原点控制，schema 2 绝对相位证书通过 | [RCWA 实际验收](local_validation/09_RCWA实际验收.md) |
| 细网格 FDTD 与 RCWA 的平面、裸光栅、+100 nm 位移反射对照通过 | [裸光栅对照](local_validation/backend_comparison_fine_bare0/backend_comparison.html)、[最终摘要](local_validation/final_validation_summary.json) |
| Script Prompt 弃用警告处理 | [API 弃用警告说明](local_validation/API弃用警告说明.md) |

## 解释这些结果时的边界

默认 dx=50、dy=30 nm 的裸光栅与 RCWA 的复矩阵 L2 差约 5.97%，所查最大相位差约 7.74°，未通过预定 3% / 2° 对照门限。dx=25、dy=15 nm 的对应控制约为 1.60% / 1.96°，通过门限；这不是全部 39 点的统一误差上界。

固定裸光栅、dy=30 nm 时，dx=50 相对 dx=25 nm 的复矩阵 L2 差为 4.6634%，dx=35 为 1.9799%（取 |m|≤8 的对角通道）。dx=25 nm 是数值对照，不是解析真值。详见[网格对照 CSV](output_ah53_overnight/mesh_dx_nm_convergence.csv)。

RCWA 统一输出 r_E/t_E 和功率归一化 S11/S21，每次导出重读十份真实证据并重算证书。S11/S21 仅含上侧零阶 s/p 入射两列，完整双侧多阶 S 矩阵不在范围内。跨后端对照只验证反射，未验证 FDTD 透射复幅值。

通宵第 24 点曾因 Windows 父 PID 复用被清理逻辑误判为失败。现场已保留，修复进程身份检查后进行了真实重跑，未直接改写成功状态；过程见实施记录。

真实验收限于 643 nm、二维周期、正入射和恒定实折射率。外部 AH53 工程接入按用户指示暂缓，SMASH 尚未实施。教学五点配置只用于学习操作，不增加上述验收范围。
