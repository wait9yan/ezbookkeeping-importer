# 后端开发规范

项目采用 Python 后台服务与 CLI，首期无独立前端。现有模板规范逐步由本次真实实现和验证替换；未填充文件不能作为既有工程事实。

## 规范索引

| 规范 | 内容 | 状态 |
| --- | --- | --- |
| [配置入口](configuration.md) | 环境变量、业务 TOML、命令依赖与严格校验 | 已统一配置契约 |
| [单次维护命令](cli-maintenance.md) | 无交互CLI、状态快照、人工决定与范围复查 | 单进程方案已实施 |
| [目录结构](directory-structure.md) | 模块和依赖边界 | 已按首期实现更新 |
| [数据库](database-guidelines.md) | PostgreSQL 事务、任务领取和迁移 | 已按首期实现更新 |
| [QQ 来源接纳](qq-source-authentication.md) | QQ 收件节点、认证服务及认证头结构 | 实际样本与拒绝路径已验证 |
| [卡号匹配与原币入账](account-matching.md) | 账户描述匹配、目标币种冻结与历史决定兼容 | 本轮实现契约 |
| [恢复与调度](recovery-and-scheduling.md) | 重试冻结事实、断连退出、邮件头预筛选与核对检查点 | 已有回归覆盖 |
| [错误处理](error-handling.md) | 明确拒绝、结果不明和恢复 | 已按首期实现更新 |
| [质量验证](quality-guidelines.md) | 测试、静态检查和构建 | 已按首期实现更新 |
| [日志](logging-guidelines.md) | JSONL、轮转、脱敏及失败行为 | 已按首期实现更新 |
| [ezBookkeeping HTTP 契约](ezbookkeeping-api.md) | 金额、分页、时区及完整结算 | 本地源码及真实 HTTP 已验证 |

## 开发前检查

阅读当前任务的 `prd.md`、`design.md`、`contracts.md` 和 `implement.md`，再读取所修改模块对应的规范。涉及账本适配、分类或结算时必须阅读 HTTP 契约；跨层改动同时阅读 `../guides/`。

## 质量检查

按针对性单元测试（整个后端测试命令硬超时 60 秒）、静态检查、构建、最小冒烟的顺序验证。运行命令及限制以项目最终建立的工具配置为准，不将模板或目录存在当作测试通过。所有新规范和任务记录使用简体中文。
