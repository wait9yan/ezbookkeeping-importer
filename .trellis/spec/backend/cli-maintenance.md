# 单次维护命令与状态快照

## 1. 范围与触发

所有维护功能在shell中单次执行，不创建console/导航菜单，也不影响后台run生命周期。CLI只负责解析、资源组装与展示，状态和动作资格在应用层实现。

## 2. 命令签名

- `ebki status|issues|sync|recheck|migrate|doctor|restore-audit`：保留已有单次命令；维护命令支持`--format json|text`，默认json。
- `ebki issues [--entity-type TYPE] [--entity-id ID] [--code CODE] [--status STATUS] [--snapshot-out FILE]`：当前诊断列表，导出固定集合。
- `ebki issues show --entity-type TYPE --entity-id ID [--code CODE]`：完整详情、动作与前置快照；多诊断全部可见。
- `ebki issues candidates --snapshot FILE [--target-id ID]`：单项候选账单、账户与分类查询。
- `ebki issues resolve --snapshot FILE --action ACTION --reason TEXT [--target-id ID | --transaction-id ID] [--account-id ID]`：单项人工决定。
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

## 稳定来源问题

### 1. 范围
月账单自动补录或日报关联存在候选、歧义、忽略和待重试来源时，使用 transaction_source 对象；不增加批次审批流程。

### 2. 签名
entity_id 为 report_key:row_key；resolve link 的 --target-id 指远端账本交易，--transaction-id 指内部交易，两者互斥且必须选一个，内部关联只用于来源对象。retry/ignore 不接受目标参数。

### 3. 契约
集中快照涵盖来源版本、报告指纹、相关来源/交易/任务状态；view 仍只展示。网络读取后再验快照，提交复用来源锁与统一关联规则。人工决定保存在来源行，不随核对投影更新删除。

### 4. 错误矩阵
| 条件 | 结果 |
| --- | --- |
| 两种目标同时存在、错误实体使用内部目标 | 参数阶段拒绝 |
| 版本或关联集合变化 | Conflict，重新获取快照 |
| 同报告两行关联同交易 | 拒绝，不破坏真实数量 |
| UNKNOWN/DISPATCHING | 不重发、不另造交易 |

### 5. 案例
正常：检查来源候选后 link 指定内部或远端交易；基础：ignore 只影响所选来源，后到独立日报不继承跨报告忽略；错误：把整个报告或全部同金额来源一并忽略。

### 6. 必需测试
目标互斥、严格JSON、过期快照、网络后状态变化、自动/人工竞争、来源唯一性、重试与核对刷新保留决定。

### 7. 错误与正确
错误：CLI直接更新关联字段绕过校验。正确：所有动作复用应用层来源关联与 issue_actions，展示和执行资格一致。

## 确认新建的候选快照契约

### 1. 范围
confirm-new用于区分同条件的独立消费，不能关闭全部查重。月报分类和发送预检必须共享相同确认判定。

### 2. 数据签名
重复诊断保存候选关键字段快照；import_decision.confirmed_candidates保存本次明确确认的快照集合。字段覆盖id、type、sourceAccountId/sourceAmount、destinationAccountId/destinationAmount、time、comment；账户及交易ID规范化为字符串。没有新增数据库表或迁移。

### 3. 契约
resolve使用当前完整问题快照，将诊断中的候选快照复制为确认集合，不凭布尔值永久放行。分类与发送预检复用duplicate_confirmation中的规则，读取确认ID的当前远端记录，即使该记录被移到普通匹配范围之外也能发现实质变化。自己的来源marker不能被确认排除。旧allow_new不构成豁免；旧重复诊断缺候选快照时，需先recheck形成当前诊断再确认。

### 4. 错误矩阵
| 情形 | 结果 |
| --- | --- |
| 已确认候选关键字段未变 | 分类及发送前一致排除该候选 |
| 新增候选或已确认候选改变 | 继续拦截，重新核实 |
| 获取候选失败 | 明确失败，不当作已删除或零候选 |
| 候选已删除 | 不再构成候选，其他候选仍正常检查 |
| 自身marker出现 | 不豁免来源核实，不创建第二笔 |

### 5. 案例
8月22日和23日的同商户4.68元不属于普通重复；同日同卡同商户同金额仍形成候选，明确确认后允许独立新建。

### 6. 必需测试
确认新建贯穿resolve→classify→write；确认后新候选、金额/账户/日期/备注变化；自身marker；旧allow_new与无快照诊断；月报与日报路径的一致性。

### 7. 错误与正确
错误：分类读取allow_new而发送前无条件拒绝任何候选；或在发送前直接跳过所有查询。正确：两阶段共享确认快照规则，只排除明确确认且未变化的候选。

### 显式目标关联兼容性
transaction_source 的 --target-id 已明确指定目标，因此直接读取该目标并校验原有交易日/入账日前后一天的查询范围、type=3、有符号CNY结算金额及唯一对应账户；不要求其备注与银行商户相同。该路径不复用自动普通候选的严格商户/同日过滤，不扩大金额/账户边界，不新增第二套候选搜索。自动补录仍使用严格候选规则。
