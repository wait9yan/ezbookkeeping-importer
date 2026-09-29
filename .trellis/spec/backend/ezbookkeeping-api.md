# ezBookkeeping HTTP 契约

本规范基于相邻 ezBookkeeping `b2a3f4ed` 源码及隔离实例真实 HTTP 验证。部署前仍须检查实际镜像版本；不修改上游数据库。

## 适用场景与接口

账本适配器集中实现账户、分类、交易查询及写入。应用层通过 `Ledger` 能力接口注入依赖，不在银行解析器或 CLI 中直接发起写账请求。

| 接口 | 关键契约 |
| --- | --- |
| `GET /api/v1/accounts/list.json` | 数组，账户 ID 为字符串，递归处理 `subAccounts`；多子账户父节点不能记账。 |
| `GET /api/v1/transaction/categories/list.json` | 按类型分组的对象，组内数组包含 `subCategories`；子级继承父级隐藏状态。 |
| `GET /api/v1/transactions/get.json` | `id` 字符串，结算前须 `with_pictures=true`。 |
| `GET /api/v1/transactions/list.json` | `count<=50`，`min_time/max_time` 为内部毫秒序列，按 `nextTimeSequenceId` 继续查询，不能仅查第一页。 |
| `GET /api/v1/transactions/list/all.json` | `start_time/end_time` 为 Unix 秒，响应为完整数组，不能套用分页包装。 |
| `POST /api/v1/transactions/add.json` | 单笔创建，`sourceAmount` 为整数分，ID 字段为字符串。 |
| `POST /api/v1/transactions/modify.json` | 完整修改请求；用于同账户金额结算及已授权 USD→CNY 同 ID 账户/金额迁移。 |

全部请求携带 Bearer Token、`X-Timezone-Name: Asia/Shanghai` 和 `X-Timezone-Offset: 480`。Token 由环境或秘密挂载传入，不写入配置示例或日志。

## 金额与类型

- 支出交易 `type=3`，对应支出分类 `type=2`；还款转账交易 `type=4`，转账分类 `type=3`。
- 明确的日报退款写负支出，不转成普通收入或再次取负。
- `sourceAmount=1234` 表示 `12.34`，不经 `float`。来源金额超两位小数明确失败；新交易以原币整数分入同币账户。
- 账户决定币种；按账户描述卡号及原币唯一匹配，新 USD 消费记入 USD 账户。已有 CNY 暂估决定只按冻结的旧契约恢复，不能将 USD 数值写人民币账户或反向混写。
- 临时分类必须唯一匹配完整路径 `其他杂项 → 待分类`，验证父子可见、二级及支出类型，不自动创建替代分类。

## 完整结算更新

回读当前交易，保留 `type/categoryId/time/utcOffset/sourceAccountId/destinationAccountId/destinationAmount/hideAmount/tagIds/comment/geoLocation`，将 `pictures[].pictureId` 转换成 `pictureIds`，`settle_amount` 只修改 `sourceAmount`；`settle_currency` 仅按冻结授权同时修改 `sourceAccountId` 与 `sourceAmount`。不能用首次导入快照覆盖用户后续分类和备注。目标不存在、来源标记不符或账户/金额发生未授权变化时明确失败，不重建。USD→CNY 迁移仍保留原 ID，回读确认后才切换本地决定。

正确示例：`payload = ledger.settlement_payload(ledger.get(target_id), actual_cents)`；完整载荷持久化后经统一写入用例发送。错误示例：仅发送 `{id, sourceAmount}`，或遗漏图片读取导致修改时清空图片关联。

## 错误矩阵

| 条件 | 行为 |
| --- | --- |
| 缺时区请求头 | 上游可拒绝为 `200008`，修复请求构造，不修改源日期绕过。 |
| 明确交易不存在 | 表达目标缺失，不能将其当作成功或自动重新创建。 |
| 网络错误、5xx、非法成功响应、重复请求结果无法核实 | 写操作结果不明，留在 UNKNOWN 并回读核实，不自动重发。 |
| 明确业务拒绝 | 保留错误码和原操作身份，修正后按决定版本重试。 |
| 分类隐藏、删除、类型变化 | 发送前再次校验，拒绝使用过期映射。 |

## 必需验证

1. 常规：创建正支出、负支出及转账，真实回读类型与金额。
2. 边界：同一秒 53 笔跨 50 笔页边界，全部 ID 恰好出现一次。
3. 结算：用户已改分类/备注后修改金额，保留所有非金额字段；图片转换单独覆盖。
4. 故障：成功响应丢失、无效响应及回读失败不得生成第二个创建请求。
5. 不存在、查询失败与重复来源标识多候选必须分开表达。

本地真实验证记录见当前任务 `research/implementation-api-review.md`；生产邮箱、模型服务和生产账本接通需另有实际证据。

新原币决定的核对及币种校验见 [卡号匹配与原币入账](account-matching.md)；USD 日报先入 USD 账户；唯一可信 CNY 结算可经 settle_currency 同时修改账户与金额，不能仅把 CNY 数值覆盖进 USD 账户。
