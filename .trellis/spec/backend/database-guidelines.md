# PostgreSQL 持久化

导入器使用独立 PostgreSQL 数据库，不读写 ezBookkeeping 业务表。`adapters/persistence/postgres.py` 提供参数化 SQL、字典行、JSONB、显式事务和 worker 排他锁。

## 事务契约

使用 `with store.transaction():` 将决定变化、任务状态和对应 `audit_events` 原子提交。`store.execute(sql, params)` 中外部值必须通过 `%s` 参数绑定；标识符动态生成仅允许专用 SQL 标识符 API。

任务领取只持有短事务，读取并锁定交易与任务，比较候选版本、任务版本和交易版本一致后改为 `dispatching`。先提交领取与尝试，再进行网络请求；禁止跨网络持有数据库行锁。

数据库提供位置、来源行、操作标识及目标关联唯一约束，不能对日期/商户/金额设唯一约束。同值真实多笔交易须保留数量。

## 恢复

- 邮件位置包含来源、文件夹、UIDVALIDITY 和 UID。下载任务先落库，之后推进扫描游标。
- `dispatching` 崩溃不能证明未发送；恢复到 UNKNOWN，通过原操作身份核实。
- 明确拒绝后修正可重试，但沿用操作身份及已冻结金额。结算重试始终恢复结算操作，不返回创建路径。
- 审计与状态同事务；普通文件日志不承担去重或恢复依据。
- 原件采用同目录临时文件写入、fsync 后原子发布，已有摘要对应文件必须逐字节一致。

## 验证

真实 PostgreSQL 测试使用随机隔离 schema，结束仅清理本测试 schema。必须覆盖决定过期、并发 worker 排他、响应丢失、重启核实、同值多笔、结算拒绝及预检失败后的重试。测试不能以 SQLite 替代 PostgreSQL 锁和事务证据。

数据库迁移为显式维护步骤；启动 worker 不应悄悄修改 schema。修改迁移文件时检查构建产物也包含同一权威迁移，避免源码和安装包各维护一份副本。

当前初始迁移的单一物理源是 `migrations/001_initial.sql`，包内 `schema.sql` 为指向它的符号链接；wheel/sdist 构建需验证展开后的 SQL 与该源逐字节一致，不能将链接改成独立副本。


## migrate 自动初始化目标库

`PostgresStore(dsn, create_database=False)` 默认不建库。仅 migrate 由共享命令能力授予 create_database；初次连接明确 missing_database 时，使用相同 DSN 的 postgres 维护库与 autocommit 创建显式 dbname，然后关闭维护连接、回连原目标再迁移。不得因认证或网络故障触发建库，不创建角色或替换目标数据库。

CREATE DATABASE 使用 psycopg.sql.Identifier；先读取服务器标识符字节限制，拒绝会截断的目标名。并发 DuplicateDatabase 可回连验证，权限及其他错误必须明确失败。建库不在事务内，表迁移仍在事务与迁移锁内。
