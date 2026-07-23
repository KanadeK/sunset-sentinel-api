# Sunset Sentinel API

Sunset Sentinel API 将第三方 API 的生命周期信号转成可执行的迁移队列。它读取标准 HTTP
`Sunset` / `Deprecation` header、OpenAPI 弃用元数据和本地 feed；随仓库提供的样例不会
发送到外部服务。

项目采用 Python 3.12、可确定性测试的领域核心、SQLite、CLI 与 FastAPI。当前源码树已经
提供包元数据和严格质量门禁；完整的 v0.1.0 使用说明将在发布里程碑交付。

公开仓库抽样检索未发现同名且高度同构的活跃项目；样本与差异化边界见
[竞品抽样文档](docs/COMPETITOR_SCAN.md)。

```bash
python -m pip install -e ".[dev]"
python scripts/verify.py
sunset-sentinel --version
```

许可证：[MIT](LICENSE)。安全问题报告方式见 [SECURITY.md](SECURITY.md)。
