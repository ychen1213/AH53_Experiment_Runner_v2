# 给 Codex 的本机实施指令（复制即可）

请检查当前目录中的AH53 Experiment Runner v2源码和README，不要把这套代码当作已经在Lumerical 2023 R2.1运行验证过的成品。

我的机器上有Lumerical 2023 R2.1，主目标是让它在无人值守时顺序完成AH53的单变量扫描，并逐点保存复Jones矩阵、功率与相位，次日自动给出对照报告。

严格按以下顺序实施：

1. **环境核验**：找到本机真实的`lumapi.py`，确认Python解释器、numpy、matplotlib、License和可用的FDTD资源；只修改JSON中的本地路径，不要轻易重装已能使用的Lumerical。可修改Python版本与依赖使之与2023 R2.1兼容。
2. **离线检查**：`python -m unittest discover -s tests -v`和`python run_experiments.py plan --config configs/quick_check.json`；查看生成的0、200、400 nm截面，检查几何操作是否符合“平坦化填充+上方膜厚”定义。
3. **一例真实运行**：执行`python run_experiments.py run --config configs/quick_check.json --limit 1 --visible`；若有LumApiError，查看该case的`worker.log`，按照2023 R2.1实际属性修正，不允许“try/except后生成假的成功result.npz”。
4. **数值/物理基准**：建立空气/无损n=1.5平面界面的复Fresnel幅值/相位对照、对称±m检查、刚性平移相位检查，验证`r_E`的参考面和s/p符号。此步通过前，复Jones相位不标记为校准完成。
5. **小扫描**：完整运行quick_check三点，看是否连续求解、逐点记录、结果物理合格；按本机实际耗时、内存与许可证能力调整超时/资源。
6. **通宵扫描**：通过后才运行`configs/overnight_example.json`；检查每个case从相同组固定基准生成，只允许指定参数发生变化。失败继续队列、下次仅重试失败case。中断后状态一致，不应跳过内容不完整的结果。
7. **报告**：检查`report.html`、`coefficients_long.csv`的每个数值都来自真实FDTD结果；监视器和源始终固定；近场色标统一；相位接近幅度零时不做有意义的相位归纳。
8. **与我已有AH53 .fsp连接**：需要新增可选已有工程适配器时，先读取真实对象树、材料与层次定义；不允许假定原有工程已有`film_stack`结构组。支持从已有文件导入配置并检查其物理等价性；不要覆盖原工程。
9. **下一阶段**：在物理接口稳定后加入RCWA后端（2023 R2.1），对同一结构输出一致定义的功率归一化S矩阵及电场幅值矩阵；随后才能进入理想SMASH光路模块。

每一步都记录你实际执行过的命令、Lumerical日志、异常与验收结论。保留所有失败现场以便定位，且绝不伪造运行结果。
