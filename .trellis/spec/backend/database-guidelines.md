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

run 与 migrate 共用数据库准备与版本迁移，完成后才开始业务工作；初始化阶段可观测并明确失败。迁移链只追加，不改已发布脚本；新增根目录脚本时同步创建包内migration_sql同名符号链接，不能复制SQL；安装包、源码和构建产物必须包含同一链。

已发布初始迁移 `migrations/001_initial.sql` 保持字节不变，包内迁移由权威目录派生/链接，不能手工维护第二份最新建表 SQL。结构契约从完整迁移链自动生成，各版本与脚本校验和一起验证。


## migrate 自动初始化目标库

`PostgresStore(dsn, create_database=False)` 默认不建库。run 与 migrate 由共享命令能力授予 create_database；初次连接明确 missing_database 时，使用相同 DSN 的 postgres 维护库与 autocommit 创建显式 dbname，然后关闭维护连接、回连原目标再迁移。不得因认证或网络故障触发建库，不创建角色或替换目标数据库。

CREATE DATABASE 使用 psycopg.sql.Identifier；先读取服务器标识符字节限制，拒绝会截断的目标名。并发 DuplicateDatabase 可回连验证，权限及其他错误必须明确失败。建库不在事务内，表迁移仍在事务与迁移锁内。

## 九表业务结构与数据契约

### 1. 范围与触发

数据库业务表结构仍来自有序迁移链。结构签名从隔离 PostgreSQL 逐版本执行权威迁移自动生成并作为只读资源打包；run、migrate 和 doctor 共用系统目录签名检查，不再运行期创建临时 schema。校验范围保留列类型/空值/默认值/生成列、CHECK/FK/唯一约束、索引及表列注释。PG18 的重复 NOT NULL 约束表示由 attnotnull 统一校验，不以宽泛字符串清洗丢弃结构差异。迁移历史校验与账务事实分离，不创建第二份业务状态机。

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
| 有完整迁移路径的已知旧 schema | 校验旧版本后依次升级；漂移或不完整结构明确失败 |
| 核对半轮失败/并发输入改变 | 保留上一完整结果，明确重试 |

### 5. 正常、基础与错误情形

正常：同原件在多个文件夹生成多个来源项、一个 email。基础：同内容重复报告只关联邮件。错误：B 邮件内容冲突时覆盖 A 的报告原件，或用无目标 ON CONFLICT DO NOTHING 吞掉交易主键碰撞。

### 6. 必需测试

真实 PostgreSQL 验证九表白名单、中文注释、全部关键 CHECK/FK/唯一性、循环 FK 提交失败、碰撞回滚、来源认证隔离、首次扫描边界、UNKNOWN 和核对并发发布。wheel/sdist 迁移链与权威源逐字节一致，并校验派生契约和链hash。

### 7. 错误与正确做法

错误：为旧模型保留 facts/parsed 副本、问题历史表、审计计数调度；逐条发布核对后删除旧项。

正确：事实只有明确权威字段，诊断直接归对象；共享完整输入快照协议在发布事务重验，再一次替换有效集合。

## 启动自动迁移与低权限校验

### 1. 范围与触发

run 的首次初始化与后续升级和 migrate 共用一套实现。配置原子生成及Docker降权仍由原入口负责，数据库准备在恢复任务、同步和任何账本写入之前完成。doctor及其他维护命令不自动迁移。

### 2. 签名与数据库记录

`PostgresStore.migrate(*, stop_event=None, hold_worker=False, progress=None) -> int` 返回实际目标版本；run通过hold_worker保留会话锁到退出。`check_schema() -> int` 在只读一致性事务中校验目标版本，`lock_worker()` 幂等获取，`unlock_worker()` 释放本会话持有锁。schema_version保存有序已应用版本、应用时间与脚本校验和；已发布v1只有版本与时间，须先严格验证001结构再接纳并升级元数据。run独有progress回调输出database_preparing、database_migration_started/completed和database_ready安全JSON事件，维护stdout仅单个结果。各版本脚本与派生签名为同一安装包资源，最新版本由链推导，不在CLI或doctor另写魔法版本值。

### 3. 契约与锁

run/migrate先取得worker会话排他锁780417，run持有到退出；锁获取须幂等，不能反复pg_try_advisory_lock造成计数泄漏。独立migrate遇到运行中worker明确冲突。每版迁移事务沿用780418→780416顺序，并在锁内重读状态、检查历史与该版本结构后执行下一项；一版的SQL和版本记录一并提交。建库在维护会话串行锁780419中重查目标存在性；等待使用try-lock轮询并尊重停止事件，最多min(connect_timeout,10秒)，超时明确拒绝，不在事务中CREATE DATABASE。维护库连接始终关闭，再回连原目标。

缺库需要CREATEDB与维护库CONNECT；空schema需相应建表权限；实际升级需目标对象修改权限。数据库已最新时只读结构和历史校验，不为校验要求CREATE。容器root不提供数据库提权，不能自动创建角色或授予权限。

### 4. 校验与错误矩阵

| 条件 | 行为 |
| --- | --- |
| 明确缺库 | 仅run/migrate按显式目标创建后初始化 |
| 已知旧版本、结构和历史正确 | 顺序执行未应用版本 |
| 最新结构、普通业务账号 | 只读验证并正常运行，无DDL |
| 旧v1基线可信但无升级权限 | 明确报错，管理员迁移后普通账号运行 |
| 被改写脚本、版本断链、缺列/索引或额外应用表 | 拒绝，保留内容，不推测补丁 |
| 数据库比程序新 | 拒绝，无自动降级 |
| 旧worker仍持锁 | 迁移冲突，禁止DDL |
| 一版迁移失败或中断 | 本版回滚，前版保留，再启动从已提交处继续 |

### 5. 正常、基础与错误案例

正常：v1库保留合成事实、执行元数据迁移后使用普通账号启动。基础：全新缺库直接run完成配置、建库和首轮同步。错误：直接改001后把旧库标成新版本，或者吞掉认证错误并试图建另一个库。

### 6. 必需测试

PG17/18派生签名再生成一致；旧库带数据、跨版本、新库结果一致；丢列/索引、错误历史、hash和版本超前；DDL权限不足、最新库无CREATE；并发初始化/升级、旧worker排他、信号回滚与重试；实际镜像默认run不预迁移，首轮导入及强杀恢复远端写入不重复。迁移资源缺失、SQL与签名不同步必须门禁失败。

### 7. 错误与正确

错误：镜像入口串联单独迁移实现；单看schema_version认为完整；每次run建临时schema要求高权限；迁移期间旧worker还在工作。正确：统一版本链、派生完整签名、按每版事务提交和worker排他，并将正常重启保持只读检查。
