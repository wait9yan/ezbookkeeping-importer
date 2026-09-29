# QQ 邮件来源接纳

## 1. 范围与触发

配置 IMAP 主机为 `imap.qq.com` 时自动用于招行直收邮件（主机大小写及末尾根点规范化）。认证服务名与真实收信节点是两个不同字段，不能要求相等。本策略信任 QQ 收件链报告的认证结果，不自行执行 DKIM 公钥验签。没有人工接纳运行模式或来源策略开关；未实现认证的 IMAP 主机返回明确来源异常。

## 2. 接口

`source_status(raw, settings, origin) -> (status, reason)`，状态为 `verified` 或 `requires_acceptance`；返回固定原因，不回显邮件头或凭据。认证结论由 `ingest` 保存到已登记的 `email_source_item`，不修改原件，不在 email 上复制认证摘要。人工接纳保存 accepted_at/acceptance_reason，不把认证结果改为 verified。

## 3. 契约

- 正式入口仅 IMAP，不支持 EML 导入；纯认证函数保留 origin 参数以拒绝没有可信采集上下文的非 IMAP 输入，正式 ingest 仅接收已登记来源项 ID；转发主题仍需来源接纳。解析器可独立处理合成邮件字节，但不能据此伪造可信采集上下文。
- 认证服务按 QQ 协议检查为 `mx.qq.com`，不再单独配置。顶层 `Received` 的真实 `by` 主机须为合法 DNS 名，等于 `qq.com` 或在 `.qq.com` 标签边界下；注释及引号内的文本不充当 by 子句。
- From 唯一且地址恰为 `ccsvc@message.cmbchina.com`；Subject 不重复；Authentication-Results 唯一。
- 认证头先处理注释与引号，再按结果段解析；SPF、DKIM、DMARC 各出现一次且均为 pass。DMARC 自身 header.from 须精确为 `cmbchina.com` 或 `message.cmbchina.com`，不能从其他属性或 reason 推断对齐。
- QQ 实际会在身份属性的域名中间折行。使用 `raw_items()` 保留原始 CRLF，仅在非引号的 `header.from`、`header.d`、`smtp.mailfrom` 值中连接真实 CRLF+WSP 分段；不删除普通空格/tab，不改写方法名或属性名。

## 4. 校验与错误

缺失、重复、冲突、未知认证结果结构、伪造域后缀、不完整注释/引号、非 QQ 顶层节点均返回 `requires_acceptance`。不能为了处理未知结构而静默认定来源可信；不要将认证异常混成解析成功或入账成功。

## 5. 正常与异常情形

正常：认证服务 `mx.qq.com`，实际收件节点 `synthetic-node.qq.com`，三项唯一 pass 且 DMARC 银行域对齐。基础：同一原件跨文件夹保留独立来源项；同一业务来源下存在可信项即允许继续，后来可信来源会重新驱动此前仅因来源阻塞的解析。异常：reason 文本中包含 pass、重复相互矛盾的方法、QQ 相似后缀主机或普通空格拆开的域名，都不能通过。

## 6. 必需测试

`tests/unit/test_qq_authentication.py` 覆盖实际 QQ 两种域名折行形状、注释/quoted reason 伪造、冲突或重复方法/属性/From/Subject、恶意主机与域名后缀、普通空白和非目标语法折行。生产验收需新鲜只读 QQ 抽样验证；真实原件仅存忽略目录，不提交到测试代码。

## 7. 错误与正确做法

错误：在整个头部搜索 `spf=pass`，把收件主机与认证服务名强制相等，或全局去掉空白。

正确：分别校验收件主机和认证服务；从结构化结果段判断唯一成功及域对齐，仅兼容已观察到的身份值折行。

认证/人工接纳与原件关联按来源项维护。采集完成项必须同时有 email_id、collected_at、source_status、source_reason；未采集不伪造认证。接纳理由、时间和重新安排解析同事务。跨不同 source_id 的同一原件当前不支持多业务上下文，明确失败，不能静默继承或覆盖。集成测试必须覆盖来源到达顺序无关、可信来源不覆盖其他项认证、接纳事务回滚和跨业务来源拒绝。
