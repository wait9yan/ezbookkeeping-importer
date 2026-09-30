# 单次维护命令与状态快照

## 1. 范围与触发

所有维护功能在shell中单次执行，不创建console/导航菜单，也不影响后台run生命周期。CLI只负责解析、资源组装与展示，状态和动作资格在应用层实现。

## 2. 命令签名

- `ebki status|issues|sync|recheck|migrate|doctor|restore-audit`：保留已有单次命令；维护命令支持`--format json|text`，默认json。
- `ebki issues [--entity-type TYPE] [--entity-id ID] [--code CODE] [--status STATUS] [--snapshot-out FILE]`：当前诊断列表，导出固定集合。
- `ebki issues show --entity-type TYPE --entity-id ID [--code CODE]`：完整详情、动作与前置快照；多诊断全部可见。
- `ebki issues candidates --snapshot FILE [--target-id ID]`：单项候选账单、账户与分类查询。
- `ebki issues resolve --snapshot FILE --action ACTION --reason TEXT [--target-id ID] [--account-id ID]`：单项人工决定。
- `ebki recheck --snapshot FILE`：仅复查固定快照集合；无参数recheck明确表示执行时全局符合条件的对象。
- FILE可为`-`以读取stdin；对象身份及已观察版本只能来自快照，不同时接受第二组实体/version参数。

## 3. 快照和输出契约

`issue_snapshot.py`集中生成、JSON规范化和校验。文档只有`snapshot_version`和`items`，版本必须为整数1；每项只有issue/state/view。issue包含entity_type/entity_id/code/detail/version/status/context；state由主行row和相关对象related组成。view只供展示，不作为payload或授权凭据。

date/datetime为ISO字符串，Decimal为精确字符串；不接受非有限数字、未知值类型或非字符串字典键。CLI JSON拒绝重复键及NaN/Infinity；单项操作恰好一项，不能自动取第一条同code诊断。

查询与写入共用`issue_actions`。resolve在短事务内锁定后重读比较状态；候选远端读取不持长锁，但全部读取后再验，提交时仍再验。背景task与关联交易按交易→task顺序，恢复路径使用相同顺序。JSON stdout只有命令结果，错误写stderr；text不解析markup或终端控制字符。

## 4. 验证与错误矩阵

| 情况 | 行为 |
| --- | --- |
| 缺失/非法JSON、重复键、版本或字段非法 | 参数阶段拒绝，资源与写入尚未开始 |
| 单项操作空/多项快照 | 明确拒绝，不自动选择 |
| 非空reason缺失、参数互斥错误 | 写入前失败 |
| link无target或其他动作带target | 拒绝；account仅供交易retry |
| 快照与当前完整状态不同 | Conflict，不自动更新版本重放 |
| 邮件其他诊断或状态变化 | 整对象操作冲突，不能只比较所选诊断 |
| 解析计算晚到，人工决定已提交 | 发布事务拒绝过期更新及其报告/交易 |
| UNKNOWN/DISPATCHING处理 | 按原规则核实/登记意图，绝不重发 |
| 精确复查部分状态变化 | 固定范围内逐项scheduled/already_pending/skipped与原因 |
| 中断 | 130/143，只影响当前维护进程；已提交结果保留，不保证零副作用 |

运行/业务失败退出1，参数错误2，正常结果0。合法批量结果中的跳过不伪装成成功安排。restore-audit可能逐步提交保守状态，不承诺整命令原子回滚。

## 5. 正常、基础与错误案例

正常：issues show输出JSON到文件，candidates只读对比，resolve link带理由关联，提交时再次校验。基础：status和issues不构建无用外部客户端。错误：执行resolve时自动读取最新版本替代用户的旧快照，或将view中伪造的payload写入数据库。

## 6. 必需测试

覆盖严格输入、JSON往返、同code多诊断、空/多项与篡改视图、版本及无版本邮件变化、网络查询后变化、单项动作参数、账户及币种、UNKNOWN不重发、范围去重/部分跳过/范围外不动、人工ignore与解析两种提交次序、事务前后真实SIGKILL。所有测试命令整体60秒硬超时，PG需认证隔离实例，不能关闭认证掩盖连接串缺密码。

## 7. 错误与正确做法

错误：CLI复制处理SQL、引入第二套动作表、持久issue ID、额外RPC或菜单；snapshot驱动数据库字段更新。正确：把快照作为乐观并发前置条件，复用resolve/recheck和同一动作判定；用当前数据库事实决定资格和结果。

## 数据库准备与就绪输出

run/migrate的数据库准备集中在Runtime与PostgresStore，migrate不再在CLI分支重复执行迁移，JSON结果为 `{"schema_version": <实际目标版本>}`。doctor在Runtime构造阶段已只读检查结构，成功结果增加 `schema_ready: true` 与 `schema_version`，保留database和原外部检查范围字段；空库、待升级、漂移、历史摘要错误或版本超前在返回成功结果前明确失败。其他维护命令同样只检查，不自动迁移；普通JSON stdout不得夹入准备阶段日志。
