# 重试、断连和核对调度契约

## 1. 范围与触发

适用于显式重试分类、写入异常关闭、PostgreSQL 断连恢复、IMAP 预筛选及核对调度。不能为提高吞吐而改变冻结事实、UNKNOWN 或来源身份。

## 2. 接口

- `Mail.fetch_headers(folder, uid) -> bytes` 与 `fetch` 共用 UID、UIDVALIDITY 校验，使用 `BODY.PEEK[HEADER]` / `BODY.PEEK[]`。
- `PostgresStore.is_connection_usable() -> bool` 检查连接是否 closed / broken；失效时 worker 退出，由进程管理器重启并重新获取锁。
- `reconcile_if_due(store, ledger, report_dir, now=None) -> bool` 返回本轮是否核对。
- `jobs` 的 `kind=reconcile_checkpoint`、`operation_key=reconciliation-checkpoint` 保存唯一核对检查点，不作为可发送任务领取。

## 3. 数据契约

检查点 payload 包含 `change={audit_count,report_count}`、`completed_at`、`next_check_at`、`queries_succeeded`。统计已提交业务审计数量，避免仅比较最大 ID 时遗漏较小序号的迟提交事务。业务变化立即核对；无变化每小时历史核对，查询失败缩短至十分钟。成功后才更新检查点。

显式 retry 的未关联 create 决定携带 `reclassify_requested`；重新分类只更新分类和分类审计，保留其余 payload、汇率快照及人工账户修正。发送中／结果不明不可触发重新分类或重发。`write_preflight_failed` 记录 `job_id` 和 `version`；完成仅关闭本操作同版本异常。旧数据缺版本仅在同任务 v1 时可证明归属，不猜测后续版本。

## 4. 校验与错误矩阵

| 情况 | 结果 |
| --- | --- |
| 非银行邮件头 | downloads 持久 ignored，可推进游标，恢复后关闭该下载异常 |
| 银行未知主题或转发已知主题 | 继续取全文，由解析／来源接纳决定 |
| 邮件头／正文失败或 UIDVALIDITY 改变 | 保留失败任务，不静默跳过 |
| 外币远端日期变化 | target_changed；仅历史 CNY 暂估允许已有授权金额结算，不改回日期；原币决定不被跨币结算覆盖 |
| 核对远端查询失败 | query_failed，十分钟重试，不报告 matched |
| 数据库失效 | 明确失败退出；重启将中断写入转 UNKNOWN 核实 |

## 5. 正常、基础和错误情形

正常：分类删除后修改规则再显式 retry，分类更新且金额不重估。基础：闲置轮询不读远端、不重写报告，重启沿用检查点。错误：远端写入成功但本地连接中断，重启必须核实来源标记而非重新 POST。

## 6. 必需测试

覆盖重试冻结金额／汇率／时间／marker／账户、分类错误明确保留、UNKNOWN 不分类；同版本预检异常关闭和其他版本不误关；日期差异与结算组合；闲置零读取和报告 mtime 不变；新月报／审计触发、迟提交较小 ID、每小时远端变化、十分钟查询重试；真实 PostgreSQL 断连后重启无重复写入；邮件头及正文分别失败后恢复、银行未知／转发、游标连续性。

## 7. 错误与正确做法

错误：缓存整个决定永不刷新分类；每 30 秒全量核对；只记录最大审计 ID；连接坏了仍循环等待；从邮件头判断失败直接丢 UID。

正确：显式重试只刷新分类；持久变化信号配合周期历史复核；连接失效退出重新获锁；先持久下载结果再推进检查点。

反向核对 `report_key:daily:transaction_id` 从查询失败恢复为 `awaiting_statement` 或 `import_pending` 时，核对项更新与对应 `reconciliation` 异常解除须同事务；不能关闭其他报告或其他异常类型。目标仍缺失继续保留异常，后续故障可重新打开。
