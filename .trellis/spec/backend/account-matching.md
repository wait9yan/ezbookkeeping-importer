# 卡号匹配与原币入账

## 1. 范围与触发

2026-09-28 用户授权以 ezBookkeeping 账户 `comment` 中银行卡号为唯一消费账户配置来源。2026-09-29 增加已授权 USD→CNY 月结：日报先原币入账，可信月账单唯一关联后同远端 ID 改至同卡 CNY 账户。自动还款通知已退出支持，不再产生账户映射或转账决定。

## 2. 接口与边界

`domain.accounts.match_account(accounts, reference, currency) -> (account_id, audit)` 是唯一匹配入口，`AccountMatchError` 提供 `code` 与 `candidate_ids`。`decision_currency(transaction)` 读取冻结币种，`legacy_cny_estimate(transaction)` 判断历史结算资格。

`Ledger.accounts()` 返回递归展开的账户，父级隐藏状态已传播到子级。应用层集中解析卡号并匹配，银行解析器仅提供来源 `card_reference` 与 `original_currency`。`sourceAmount` 保持官方接口整数分格式，不把自定义币种字段发送给账本。

## 3. 契约

- 描述中的卡号为独立完整数字串，可含空格或连字符分组；长度为 12–19 位。不能把两个号码或日期拼接成卡号，也不猜描述中的任意短数字。
- 邮件完整卡号精确匹配；仅四位则匹配尾号。再按原币过滤，必须唯一命中可见、`type=1` 的可记账账户，不能选多币种父账户。
- 当前新交易支持 CNY 与 USD，直接以原币金额入同币账户，不调用汇率接口；首次创建不跨币种，后续仅允许下述银行实际结算迁移。同币退款自动按负支出处理，仍执行来源校验与查重，无单独退款归属开关。
- 决定通过 `target_currency`、`account_match={account_id,card_last4,match_method,currency}` 持久保存目标币种与最小匹配依据；不复制完整银行卡号或完整账户描述。重试保留冻结的金额、币种、账户及来源标记。
- 已有旧决定无新币种字段时按明确的旧 CNY 契约读取；仅旧 `rate_snapshot` 暂估决定继续人民币结算。不得把旧人民币金额写入 USD 账户。
- 同币月结（如 CNY 账户与 CNY 结算）比较银行结算金额；USD 原币交易遇到 CNY 结算时仅在可信双方唯一关联后创建 settle_currency，同远端 ID 修改为同卡 CNY 账户及实际 CNY 金额。人工换账户也必须保持决定币种，并以 `account_override` 明确记录覆盖，不把人工决定伪装成自动匹配。

## 4. 错误矩阵

| 情况 | 行为 |
| --- | --- |
| 未找到卡号／同卡但币种不同 | 明确匹配失败，不换币、不借用另一张卡 |
| 同尾号同币种多账户 | 明确歧义，不按列表顺序选第一个 |
| 完整号不同但尾号相同 | 完整号不得降级成尾号匹配 |
| 账户隐藏、删除或币种变化 | 发送前校验失败，保留冻结决定 |
| 原币与结算币种不同 | 分别保存核对事实，不直接覆盖金额 |

## 5. 正常、基础与错误情形

合成示例：尾号 1234 + CNY 匹配银联 CNY，尾号 5678 + USD 匹配 Visa USD。基础：描述编辑不会令已排队决定自动换账户。错误：仅有 5678 的 USD 账户时，5678 + CNY 不得记入该账户；也不能改记到 1234 的 CNY 账户。

## 6. 必需测试

完整号与尾号、分组卡号、无卡号、多个卡号、重复尾号、同卡多币、隐藏子账户与父账户；USD 正负金额不请求汇率；账户币种变化及人工修正；重试冻结与 UNKNOWN；USD 日报先入账、月结同 ID 改 CNY 账户与金额、保留最新非结算字段；重复/UNKNOWN/目标缺失不重建；旧人民币暂估继续结算；非法配置不泄露输入。

## 7. 错误与正确做法

错误：`comment.endswith(last4)` 加取首个匹配；无匹配时默认人民币账户；仅凭原币是 USD 就执行人民币结算，或把 CNY 数值写进 USD 账户。

正确：集中提取候选完整号，按来源标识和币种得到唯一可记账账户；持久化入账币种，在发送和核对时区分原币决定与历史暂估决定。

## USD→CNY 月结契约

`background_task.task_type=settle_currency` 与 `settle_amount` 分离，前者冻结原/目标账户、币种、金额、决定版本、远端 ID 和结算报告行引用。确认前 `import_decision` 保持已确认的 USD 决定；回读确认后才同事务更新当前决定、版本、`settlement_adjustment` 及任务/尝试。原始 `original_*`、来源 marker、远端 ID 不变。结算任务冻结目标账户的最小 `account_match` 依据，确认时与 payload/target_currency 一起切换，并移除已过期的 `account_override`，避免当前决定仍声称使用旧 USD 账户。

| 条件 | 结果 |
| --- | --- |
| 双方唯一、同卡唯一可见 CNY 账户、远端仍为冻结原状态 | 构造完整修改请求，只改账户与金额 |
| 远端已达冻结目标 | 零发送确认，不伪造写入尝试 |
| 候选歧义、缺账户、目标缺失或已发生未授权账户/金额变化 | 明确问题，不猜测、不覆盖、不重建 |
| 响应丢失或崩溃 | UNKNOWN，只回读同 ID 核实 |

正常示例：USD 10.00 先入账，月结 CNY 70.00 后同 ID 修改；基础示例：重复月报不再次发送；错误示例：将 70.00 直接写入原 USD 账户。核对金额仅同币时求差额，迁移前允许 expected=CNY、actual=USD，不能伪报匹配。

## 月账单独立补录
用户已授权无日报的消费/退款按月账单 CNY settlement_amount 入同卡人民币账户，复用 match_account 唯一性规则。决定金额依据为 statement_settlement，不能伪造 original_currency=CNY 或覆盖原币金额。日报先到仍遵循原币入账/后续结算；月报先到后收到日报只补证据，不自动改回外币或覆盖用户修改。还款不纳入。

查重账户范围与可记账目标资格分离：同卡隐藏账户仍可能含历史账，不能因不可写而从查重剔除。已知来源 marker 优先于金额/账户相似筛选；远端同 marker 被改金额或账户时保留候选并报核实问题，不能将其解释为零候选再次创建。

## 日报月报结构关联与跨币结算

### 1. 范围与触发
日报先按原币入账；可信月报按原交易金额关联已有消费，再用银行CNY结算额更新同远端ID。无已有消费时月报才补录CNY。描述不同和月报原币币种为空不单独阻止唯一结构关联。

### 2. 签名
`sources.match_key(row)` 使用 event_type、occurred_date、card_reference末四位、original_amount，不使用商户；`source_candidates`保留结构候选，`automatic_candidates`尊重既有绑定/数量并用辅助描述去歧义；`source_compatible`校验已知原币与完整卡号冲突。
`statement_candidates.filter_candidates(accounts,row,records,markers=(),decision=None)`供日报/月报分类和发送前复查共享；`query_candidates`负责完整查询窗口和审计字段。候选不是新建许可，也不证明可自动改写手工账。

### 3. 数据契约
- 同交易日、同卡、同消费/退款类型、同银行原交易金额的一对一/双向唯一来源可自动关联，商户是辅助证据。明确原币冲突仍保留为待核实，不能当零候选另建；完整卡号不因末四位相同而丢失冲突。
- 月报original_amount与日报原币金额比较；settlement_amount/CNY作为银行结算依据。月报original_currency=None不伪造USD，也不阻止与可信日报USD事实的唯一关联。
- 普通远端候选限定上海交易日、同卡CNY/USD账户（包括隐藏历史账户）、type=3及同符号对应金额。USD按原交易额、CNY按银行结算额；已知原币按原币比较。不同备注/商户不排除候选，禁止将USD原交易额直接与CNY结算额比较。
- 已知marker独立保留以核实改过日期/账户/金额的目标；当前账面CNY不能覆盖持久原币来源。元/分用Decimal和整数精确转换，不估算汇率、不加金额容差。
- 月报先补CNY后到日报只补来源，保留初始身份/已确认CNY决定；不能新增USD或改回USD。来源唯一性保留同值多笔数量，不以结构组合建数据库唯一键。
- 已有旧CNY暂估决定尚无bank_settlement_amount时，decision参数提供已冻结的同币种payload金额作为额外查重证据；不重新估汇率，不把其CNY金额当USD原额。

### 4. 校验与错误
唯一结构来源且无已知冲突→自动关联并进入既有结算；多对一/一对多无法可靠分配、币种/完整卡号冲突→来源问题；纯远端候选→重复候选等待显式决定；查询失败→查询诊断，不新建。
已改账户/金额偏离冻结from、目标缺失或目标身份变化→既有结算预检失败，不覆盖或创建替代交易。UNKNOWN只核实，不能盲重发。手工confirm-new只豁免快照中未变化候选，新的或被改候选仍阻止发送。

### 5. 示例
正常：日报21.90USD与月报original21.90/币种空/settlement147.27CNY/不同描述自动关联，1次create+同ID1次modify，最后CNY14727分。
基础：月报先创建CNY147.27，日报后到复用该身份，无新增与改回USD。
错误：因21.90与147.27不同或商户长短差异再次补录；以任意汇率估算决定是否同笔。

### 6. 必需测试
真实隔离PG运行到write：两种来源到达顺序、同币与跨币不同描述、原币空、真实同值多笔/双向竞争、明确币种与完整卡号冲突；断言内部/source数量、create/modify次数、最终remote ID/账户/金额、原币事实及用户字段。
候选覆盖USD原额和CNY结算额、隐藏账户、不同备注、分类后发送前新增候选；恢复覆盖响应丢失/重启/已达目标/UNKNOWN/被改marker。只有candidate存在的单元断言不能代替跨币流水线验收。

### 7. 错误与正确
错误：把描述不同过滤成无候选，或因币种未明拒绝全部正常结算；只测候选不跑最终修改。
正确：原金额做结构关联，银行结算额做同ID修改；真正缺失且完整查重无冲突才新增，真正歧义暂停。

## 原交易额对不上时的剩余配对与同卡结算

### 1. 范围与触发
2026-11-09 用户授权：原交易额或交易日对不上的境外行做剩余配对，并放宽同卡换账户的结算预检。
精确结构匹配（交易日/卡尾号/类型/原交易额）失败的月报境外行（`extra.country` 非 `CN`）。2026-11-09 生产核实：国内行均为 `CN`，境外为 `US`/`HK`；已关联日报月报交易日无偏差。

### 2. 签名
`sources.residual_pool(row, transactions, *, missing)`：同卡尾号、同类型、交易日差 ≤ `RESIDUAL_DAYS`(1)、尚无 `missing` 类型（monthly/daily）来源的交易，不比较金额。`residual_candidates(rows, transactions, bindings, candidates) -> (resolved, blocked)`。`unsettled_usd(tx)`：决定币种 USD 且无 settlement_adjustment。`link_eligible(row, tx, transactions, *, missing)` 为 link_source 自动与人工关联的唯一资格判断。`_source_rows` 每项带 `_report_type`。

### 3. 契约
- 未结算 USD 候选与境外行双向唯一 → matched，走既有 settle_currency 同 ID 改 CNY；details.match_basis=`residual_unsettled_usd`。同日只靠结构字段；跨一天还要求规范化商户一方为另一方前缀（`residual_match`），否则仅暂停。
- 剩余配对与人工 link 的放宽只适用于境外月报行，以及外币日报对应来源为境外月报行的交易（`has_foreign_monthly`）；国内行仍只认精确候选。放宽只针对金额与交易日，所有路径仍须 `source_compatible`：完整卡号不同或已确认原币不同一律拒绝。
- `--target-id` 外部关联只在存在精确内部候选时拒绝；剩余配对候选不拦截，否则被暂停的境外行在近日交易其实是另一笔时没有入账出路。
- 多候选，或无 USD 候选但近日仍有未关联月报的日报交易（含 CNY 日报）→ ambiguous，不查账本、不补录；来源问题为 `source_ambiguous` 并列候选。
- 外币日报后到、精确无候选：`reverse_daily_pool` 排除本报告已关联与精确候选占用的交易、且跳过已关联或已 ignore 的行后，行与交易双向唯一则关联（保留 CNY 决定）；多个或同报告多行争同一交易则不建，来源问题报 `source_ambiguous` 并列候选；零个才新建。不得因同报告多行而抛错使整份日报失败。国内月报补录不得吞掉后到的外币日报。
- 结算预检（仅 settle_currency）：远端 type/sourceAmount/time 与冻结 from 一致，账户为可记账、同决定币种的同卡账户时放行（`accounts.same_card`：四位比尾号，完整卡号全等，与 `match_account` 一致）；dispatch 时写 `payload.observed_from_account_id`，adjustment.from 优先用它。远端已是目标状态（already_applied）且从未 dispatch 时无法观察原账户，adjustment.from 保留冻结决定的账户。
- 不估汇率、不加金额容差；普通 CNY 交易金额差异只报告。

### 4. 错误与正确
错误：原额不等即判无候选并补录第二笔；账户 ID 变化即永久预检失败。正确：近日未结算 USD 唯一则同 ID 结算，不能唯一则暂停等人工 link。

### 5. 必需测试
tests/integration/test_usd_residual.py：原额不同、交易日差1天、双候选暂停后手工关联、远距日期拒绝关联、跨天商户无关暂停、国内行拒绝剩余关联、国内补录不吞外币日报、外币日报后到双候选报歧义、CNY 日报金额不同暂停、剩余关联拒绝原币冲突、同报告两笔外币日报争同一月报交易时暂停且日报接纳不失败、ignore 一行后另一行可关联、暂停的境外行可 --target-id 关联手工账、旧暂估结算不放宽换账户、无日报补录一次、月报先到 USD 日报后到、同卡账户迁移放行、他卡/改金额/改时间拦截。
