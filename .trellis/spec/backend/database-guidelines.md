# PostgreSQL 持久化

导入器使用独立 PostgreSQL 数据库，不读写 ezBookkeeping 业务表。`adapters/persistence/postgres.py` 提供参数化 SQL、字典行、JSONB、显式事务和 worker 排他锁。

## 事务契约

使用 `with store.transaction():` 将决定变化、任务/尝试状态和对应对象当前诊断原子提交。九表模型不保存独立审计或问题表。`store.execute(sql, params)` 中外部值必须通过 `%s` 参数绑定；标识符动态生成仅允许专用 SQL 标识符 API。

任务领取只持有短事务，读取并锁定交易与任务，比较候选版本、任务版本和交易版本一致后改为 `dispatching`。先提交领取与尝试，再进行网络请求；禁止跨网络持有数据库行锁。

数据库提供位置、来源行、操作标识及目标关联唯一约束，不能对日期/商户/金额设唯一约束。同值真实多笔交易须保留数量。

## 恢复

- 邮件位置包含来源、文件夹、UIDVALIDITY 和 UID。下载任务先落库，之后推进扫描游标。
- `dispatching` 崩溃不能证明未发送；恢复到 UNKNOWN，通过原操作身份核实。
- 明确拒绝后修正可重试，但沿用操作身份及已冻结金额。结算重试始终恢复结算操作，不返回创建路径。
- 当前诊断、最新人工理由及状态同事务；普通文件日志不承担去重或恢复依据，不承诺完整业务变更历史。
- 原件采用同目录临时文件写入、fsync 后原子发布，已有摘要对应文件必须逐字节一致。

## 验证

真实 PostgreSQL 测试使用随机隔离 schema，结束仅清理本测试 schema。必须覆盖决定过期、并发 worker 排他、响应丢失、重启核实、同值多笔、结算拒绝及预检失败后的重试。测试不能以 SQLite 替代 PostgreSQL 锁和事务证据。

数据库迁移为显式维护步骤；启动 worker 不应悄悄修改 schema。修改迁移文件时检查构建产物也包含同一权威迁移，避免源码和安装包各维护一份副本。

当前初始迁移的单一物理源是 `migrations/001_initial.sql`，包内 `schema.sql` 为指向它的符号链接；wheel/sdist 构建需验证展开后的 SQL 与该源逐字节一致，不能将链接改成独立副本。


## migrate 自动初始化目标库

`PostgresStore(dsn, create_database=False)` 默认不建库。仅 migrate 由共享命令能力授予 create_database；初次连接明确 missing_database 时，使用相同 DSN 的 postgres 维护库与 autocommit 创建显式 dbname，然后关闭维护连接、回连原目标再迁移。不得因认证或网络故障触发建库，不创建角色或替换目标数据库。

CREATE DATABASE 使用 psycopg.sql.Identifier；先读取服务器标识符字节限制，拒绝会截断的目标名。并发 DuplicateDatabase 可回连验证，权限及其他错误必须明确失败。建库不在事务内，表迁移仍在事务与迁移锁内。

## 九表初始化及数据契约

### 1. 范围与触发

数据库模型重构只支持空库初始化与相同结构的重复初始化；重复初始化从权威 SQL 在事务内的随机临时 schema 构造 pg_catalog 签名，比对列类型/空值/默认值/生成列、CHECK/FK/唯一约束、索引及表列注释，而非另写预期 schema。维护账号需要目标数据库 CREATE 权限创建该 schema，检查完成删除、异常事务回滚。普通 worker 不执行结构验签。不升级旧库、不创建兼容视图，不删除运行中数据库。

### 2. 数据库接口

恰好九表：`email_sync_checkpoint`、`email_source_item`、`email`、`bank_report`、`bank_transactions`、`background_task`、`ledger_write_attempt`、`bank_statement_reconciliation`、`schema_version`。所有表、列必须有可由 pg_catalog 读取的简短中文业务标题。COMMENT 只写实体或字段业务名称（如“银行原币金额”“来源认证状态”），不写整句解释、NULL、枚举或算法；详细契约留在设计/规范文档。仅调整标题时直接应用 COMMENT 更新现有库，保留数据和结构。

### 3. 数据与事务契约

- 来源项位置唯一键为 `(source_id, folder, uid_validity, uid)`；注册与扫描检查点同事务。固定首次上界 `initial_scan_upper_uid`，完成展示由上界内来源项派生，增量不能扩大首次范围。
- `email.id` 是原件 SHA-256；邮件头 ID 为可空 `header_message_id`。来源认证及人工接纳仅在具体 `email_source_item`；email 不保存 parsed 业务副本。
- `bank_report` 保存已接纳业务头和 content；规范化内容指纹包括控制总额，排除邮件头及 locator。`email(id,report_key)` 与 `bank_report(source_email_id,report_key)` 延迟复合 FK 验证配对。
- `bank_transactions` 是日报/还款来源事实，月报行不生成交易；原币事实、当前决定、导入结果分别存储。交易经报告/行追溯 source_email_id，不建立交易证据表。
- ID 为 SHA-256(`report_key + ":" + report_row_key`) 前 12 原始字节的 Base64URL 无填充编码；两个键禁止冒号；marker 恒为 ebki- 加 16 字符 ID。
- `background_task` 统一保存 sync/sync_range/create/settle_amount/settle_currency。`ledger_write_attempt` 记录发送前 unknown、确切请求和决定版本；恢复既有目标可以零尝试完成。
- 核对完整集合、过期项清理、报告摘要及必要结算任务原子发布；网络期间不持锁，发布前重验完整输入和 reconciliation_version。
- 问题从业务对象只读聚合，无全局问题 ID/问题历史；最新人工理由只保留一份，不用无限事件 JSON 代替审计。

### 4. 校验与错误矩阵

| 条件 | 行为 |
| --- | --- |
| 相同报告/行重复 | 幂等复用 |
| 不同身份发生短 ID 碰撞 | 明确失败且报告事务回滚 |
| 同报告内容冲突 | 冲突诊断留在邮件，不覆盖接纳内容 |
| 同原件跨不同业务 source_id | 明确失败，不覆盖已有业务上下文 |
| 旧或不完整 schema | 初始化明确失败，不自动重命名或补成成功 |
| 核对半轮失败/并发输入改变 | 保留上一完整结果，明确重试 |

### 5. 正常、基础与错误情形

正常：同原件在多个文件夹生成多个来源项、一个 email。基础：同内容重复报告只关联邮件。错误：B 邮件内容冲突时覆盖 A 的报告原件，或用无目标 ON CONFLICT DO NOTHING 吞掉交易主键碰撞。

### 6. 必需测试

真实 PostgreSQL 验证九表白名单、中文注释、全部关键 CHECK/FK/唯一性、循环 FK 提交失败、碰撞回滚、来源认证隔离、首次扫描边界、UNKNOWN 和核对并发发布。wheel/sdist SQL 与 migrations/001_initial.sql 逐字节一致。

### 7. 错误与正确做法

错误：为旧模型保留 facts/parsed 副本、问题历史表、审计计数调度；逐条发布核对后删除旧项。

正确：事实只有明确权威字段，诊断直接归对象；共享完整输入快照协议在发布事务重验，再一次替换有效集合。
