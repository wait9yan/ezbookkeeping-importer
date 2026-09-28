# QQ 邮件来源接纳

## 1. 范围与触发

用于 `source_policy=qq_authentication_results` 的 IMAP 招行直收邮件。认证服务名与真实收信节点是两个不同字段，不能要求相等。本策略信任 QQ 收件链报告的认证结果，不自行执行 DKIM 公钥验签；`manual_acceptance` 可显式关闭自动接纳。

## 2. 接口

`source_status(raw, settings, origin) -> (status, reason)`，状态为 `verified` 或 `requires_acceptance`；返回固定原因，不回显邮件头或凭据。接纳结论由 `ingest` 保存到消息记录，不修改原始邮件。

## 3. 契约

- 非 IMAP 来源、手工策略及转发主题始终需要手工接纳。
- `trusted_authserv_id` 必须为 `mx.qq.com`。顶层 `Received` 的真实 `by` 主机须为合法 DNS 名，等于 `qq.com` 或在 `.qq.com` 标签边界下；注释及引号内的文本不充当 by 子句。
- From 唯一且地址恰为 `ccsvc@message.cmbchina.com`；Subject 不重复；Authentication-Results 唯一。
- 认证头先处理注释与引号，再按结果段解析；SPF、DKIM、DMARC 各出现一次且均为 pass。DMARC 自身 header.from 须精确为 `cmbchina.com` 或 `message.cmbchina.com`，不能从其他属性或 reason 推断对齐。
- QQ 实际会在身份属性的域名中间折行。使用 `raw_items()` 保留原始 CRLF，仅在非引号的 `header.from`、`header.d`、`smtp.mailfrom` 值中连接真实 CRLF+WSP 分段；不删除普通空格/tab，不改写方法名或属性名。

## 4. 校验与错误

缺失、重复、冲突、未知认证结果结构、伪造域后缀、不完整注释/引号、非 QQ 顶层节点均返回 `requires_acceptance`。不能为了处理未知结构而静默认定来源可信；不要将认证异常混成解析成功或入账成功。

## 5. 正常与异常情形

正常：认证服务 `mx.qq.com`，实际收件节点 `synthetic-node.qq.com`，三项唯一 pass 且 DMARC 银行域对齐。基础：同一原件从文件导入仍需要接纳。异常：reason 文本中包含 pass、重复相互矛盾的方法、QQ 相似后缀主机或普通空格拆开的域名，都不能通过。

## 6. 必需测试

`tests/unit/test_qq_authentication.py` 覆盖实际 QQ 两种域名折行形状、注释/quoted reason 伪造、冲突或重复方法/属性/From/Subject、恶意主机与域名后缀、普通空白和非目标语法折行。生产验收需新鲜只读 QQ 抽样验证；真实原件仅存忽略目录，不提交到测试代码。

## 7. 错误与正确做法

错误：在整个头部搜索 `spf=pass`，把收件主机与认证服务名强制相等，或全局去掉空白。

正确：分别校验收件主机和认证服务；从结构化结果段判断唯一成功及域对齐，仅兼容已观察到的身份值折行。
