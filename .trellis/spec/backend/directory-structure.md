# 后端目录与依赖

应用入口为 `src/ezbookkeeping_importer/`，采用领域、应用、适配器及入口分层。无网页服务。

- `domain/models.py` 保存解析边界的不可变 Pydantic 模型，`identity.py` 统一交易 ID 与规范化报告指纹，`money.py` 负责 Decimal 金额，`errors.py` 定义业务和外部结果错误。
- `application/collect.py`、`parse.py`、`classify.py`、`write.py`、`reconcile.py`、`resolve.py`承载采集到人工处理的业务用例；`issue_interaction.py`负责共享动作规则，`issue_snapshot.py`负责单次问题查询、规范化快照与并发前置条件；`recheck.py`只安排用户主动请求的一轮重复候选复查，复用原pending流水线。`service.py`组合一次后台周期，`maintenance.py`负责当前问题投影、共享分组统计、CLI状态及恢复核实；`records.py`负责明确事实列到解析边界的内存投影，不持久化旧facts副本；`events.py`统一结构化运行事件与安全展示契约。
- `application/ports.py` 定义实际需要的外部能力；业务代码不导入具体 HTTP、IMAP 或 PostgreSQL 实现。具体依赖仅在 `bootstrap.py` 组装。
- `adapters/banks/cmb/parser.py` 处理招行模板；`mail/imap.py` 处理只读 IMAP；`ezbookkeeping/client.py` 与 `llm/openai.py` 处理外部请求；`persistence/` 处理数据库，`adapters/evidence_store.py` 处理原件存储。
- `config.py` 合并业务 TOML 与运行环境变量，按命令依赖校验；来源契约见 [配置规范](configuration.md)。根目录`run`只定位项目并委托uv加载.env；`entrypoints/cli.py`分发命令，`entrypoints/run.py`管理同进程Runtime与信号，`entrypoints/worker.py`运行后台周期，不创建控制台或子worker；不得复制分类、写入或恢复状态机。
- `entrypoints/presentation.py`仅将同一命令结果渲染为中文摘要、Rich表格和提示；问题分组复用application/maintenance.py的纯聚合。单次CLI默认JSON、显式text，不在展示模块查询数据库、改变状态或定义另一份任务资格规则。

`tests/unit/` 只测纯逻辑和可控边界，`tests/integration/` 验证真实 PostgreSQL 与显式启用的隔离账本。个人邮件仅留在已忽略的 `email/`，夹具采用合成内容。

新增银行时实现解析边界，复用六个业务用例；不要为新银行另建分类或外部写入流程。跨层改动必须追踪输入事实、持久决定、请求、回读及异常投影。
