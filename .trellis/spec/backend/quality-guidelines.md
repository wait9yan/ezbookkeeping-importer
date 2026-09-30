# 后端质量验证

## 工具与顺序

项目固定依赖于 `pyproject.toml` 与 `uv.lock`。验证顺序：针对性后端测试 → Ruff / mypy → Python 包构建 → Docker 构建与最小冒烟。

```sh
uv sync --frozen
uv run python -c 'import subprocess; subprocess.run(["pytest", "tests/unit", "-q"], timeout=60, check=True)'
uv run ruff check src tests
uv run mypy src
uv run python -m build
```

所有后端测试命令整体设置 60 秒硬超时。需要网络的本机隔离数据库验证可能需要沙箱授权；失败应明确说明，不能用跳过测试当作通过。

## 测试边界

- 单元测试使用合成邮件，不提交个人原件，不把研究脚本当应用解析器。
- PostgreSQL 集成测试由 `EBKI_TEST_DATABASE_URL` 显式提供测试实例，采用隔离 schema。
- `tests/integration/test_live_ledger.py` 仅在指定 `EBKI_LIVE_CONTEXT` 后对隔离测试账本创建合成记录。上下文含 `base_url/token/accounts/categories`，不得放入仓库或指向生产账户。
- 未配置外部依赖时明确 skip；最终报告分别列通过和跳过，不合并为完整接通。

## 必须保持的不变量

原始金额和时间不能由模型改变；同值多笔保留数量；外部结果不明不可盲重发；过期决定不能发送；重试金额冻结；同账户结算只改金额，已授权 USD→CNY 结算仅改账户与金额且同 ID；未知来源需要明确接纳；配置与接口故障不伪装有效默认值。

最终审查检查第二真源、隐藏回退、重复状态机、宽泛异常吞错、历史修改范围绕过及秘密泄漏。变更前后需要说明行为和证据，不以目录齐全或服务启动作为验收。

## 多进程数据库测试连接

隔离PostgreSQL夹具必须向子进程传递原始`isolated_dsn`，保留随机schema选项与测试凭据。`connection.info.dsn`会移除密码，不能据此重建认证连接；也不要为使测试通过而关闭数据库认证。父子锁与收尾测试必须在要求密码的临时数据库实际执行。连接串只在测试进程和临时配置中传递，不打印或提交凭据。
