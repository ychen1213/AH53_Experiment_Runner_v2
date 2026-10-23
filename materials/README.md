# 本机材料库

此目录存放本机 `.mdf` 及其导出记录，文件默认不进入 Git。用户的真实材料库不随公开代码上传。

没有自己的库时，可用已安装的 Lumerical 生成教学材料库（复制内置材料，不修改安装目录）：

```powershell
& $lumPython tools/materials.py export-demo --api-path 'D:/Ansys Inc/v252/Lumerical/api/python' --output materials/ah53_demo.mdf
```

它包含 `AH53_TEST_Silicon`、`AH53_TEST_Silica`、`AH53_TEST_Gold`，名称明确标记为测试材料，并保存来源与拟合设置。导出不等于求解或精度验收；运行前使用 `tools/materials.py check` 检查目标波段。

该命令拒绝覆盖已有文件。换成自己的库时，把配置顶层 `material_databases` 指向你的文件，再按材料的完整名称填写 `substrate_material`、`ridge_material` 和膜层的 `material`。
