# 备份与归档

此目录只保存已经退出 `.github/workflows/` 的旧工作流及其配套文件。

- `legacy_registration_workflows_20260808/`：CloakBrowser、RuyiPage、V2、V3、V4 五套旧注册工作流。
- 仍被 V5、V6、邮箱验证或题图采集复用的源码已统一归类到 `workflow_modules/`，归档内另保留操作前快照。
- GitHub Actions 只扫描 `.github/workflows/`，所以这里的 YAML 不会出现在 Actions 运行列表中。

归档前还创建了仓库外完整备份；归档内的 `README.md` 和 `MANIFEST.json` 记录恢复方法及文件校验值。
