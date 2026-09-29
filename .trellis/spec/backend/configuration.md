# 配置入口与命令依赖

2026-09-20 用户授权统一配置来源。应用只读取 TOML 和进程环境，不自动搜索 `.env`，不再按秘密/非秘密拆分同一服务配置。

## 来源契约

- `config.toml`：业务策略、还款历史映射、商户规则、分类模式、时区、日志级别；`mail.source_id` 为稳定业务身份。
- 进程环境：数据库；账本地址/Token；AI 地址/模型/Key；IMAP 主机/端口/用户名/授权码。
- `.env.example` 列出环境变量和默认值；本地`./run`定位项目根目录并委托uv显式加载根.env，零参数选择`ebki run`、有参数透传CLI。原始`uv run --env-file .env ebki ...`继续可用，config.toml已有默认值。已有进程环境优先，应用不引入第二个dotenv加载器。
- Compose 显式映射同一组变量，但不能用 `${TOKEN:?}` 预先要求所有凭据，否则数据库维护命令无法执行。Docker 网络是部署层必需条件，独立于应用的命令依赖。
- 移入环境变量的旧 TOML 键必须报出字段名与对应环境变量，不能静默覆盖、丢弃或按环境缺失回退到 TOML。

## 命令能力

配置校验与依赖组装共享同一份命令能力判定，不能分别维护两套命令列表。

| 命令 | 能力要求 |
| --- | --- |
| migrate | 数据库与显式数据库初始化能力；仅缺库时经同实例维护库创建目标 |
| status、issues、sync、recheck | 数据库；不构造不使用的外部客户端和文件目录；各业务命令独立组装依赖。recheck只安排一次复查，真正查重和写入由worker执行 |
| issues 内部本地处理 | 数据库 |
| issues 内部候选查询、关联、账户修正；restore-audit | 数据库与账本 |
| run、worker | 数据库、账本、IMAP、流水线与存储；AI 模式额外要求 AI 服务；run在worker子进程内构造业务Runtime，父进程只负责交互与生命周期 |
| doctor | 检查完整 worker 连接配置，实际探测仍只包括已有数据库和账本读取；不声称 IMAP/AI 已接通 |

## 验证与错误

必需项缺失和非法输入在资源创建前失败。错误只能报告字段名、变量名和安全原因，不输出输入值、DSN、Token 或含凭据 URL。rules_only 不要求 AI；仍要拒绝已经提供但语法非法的环境值。URL、端口及日志级别通过统一配置边界校验。账本、AI与IMAP客户端超时固定30秒并传入实际客户端，不提供环境变量或TOML覆盖；数据库连接超时仍按原契约。

正确：迁移只提供 `EBKI_DATABASE_URL`，业务 TOML 合法即可；worker 缺模型配置时在启动阶段一次列出缺项。

错误：环境缺失时悄悄使用 TOML 的旧 Key；每笔交易处理到 AI 时才发现启动配置不完整；在构造数据库之后才检查邮箱密码。

## 路径与部署

本地运行目录固定为相对工作目录的 `data/email`、`data/reports`、`data/logs`，容器工作目录为 `/app`，对应宿主机 `./data/*` 挂载。目录及轮转参数不接受 TOML 或环境覆盖；TOML 旧字段明确拒绝，内部 Settings 仍可供测试注入。日志固定按 10 MiB 轮转，保留 5 个归档。网络名直接配置在 Compose，用户默认继承 Dockerfile 的 `10001:10001`，需要时直接设置 Compose 的 `user`；宿主机挂载目录需匹配写权限。秘密不打包；sdist 包含 `.env.example`，不包含 `.env` 或运行配置。

## 必需回归

环境变量实际生效；业务规则保留；旧键明确迁移；必需项和非法值不泄露；按命令依赖矩阵；rules_only 无 AI；固定30秒超时实际传递且旧环境变量不能覆盖；资源构造失败清理；实际 uv 加载合成 `.env`；Compose 空凭据配置渲染；固定路径挂载一致；日志级别仅从 TOML 读取；旧目录和轮转 TOML 字段拒绝。

普通消费不再接受 `accounts` 配置，改从 ezBookkeeping 账户描述解析卡号，再按邮件原币唯一匹配。新流程不用汇率，不保留汇率时效配置或专用迁移检查。旧账户映射必须明确提示迁移，不能与远端描述共同构成两个映射来源；契约见 [卡号匹配与原币入账](account-matching.md)。

## 2026-09-28 自动运行与扫描配置

worker 默认自动写入通过来源、分类、账户及查重校验的任务，退款直接按负支出处理。删除 writes_enabled、refund_ownership_confirmed 及其附属 historical_boundary_reviewed；旧键必须明确提示删除，不静默迁移。暂停使用停止 worker，恢复先停止 worker 并执行 restore-audit。

IMAP 为唯一正式采集渠道，QQ 仅为服务商与专用认证策略；删除 import-eml 命令及其能力分支，原件文件格式仍为 .eml。首次常规同步全量，后续 UID 增量，跨日按 mail.rescan_days 回扫（严格非负整数，默认7，0禁用）。UIDVALIDITY变化重新全量，有界手工补扫不改变常规游标。

source_policy 与 trusted_authserv_id 已删除，旧键明确迁移报错。来源认证按 mail.host 自动选择已实现的邮箱适配；当前内置 imap.qq.com，未知主机不继承 QQ 信任。人工 accept-source 仅用于具体来源项的异常处理，记录理由和时间，不改写 requires_acceptance 认证结论；同一业务来源下其他可信来源可驱动原件继续处理。

## 日志级别与控制台

TOML 顶层 `log_level` 对应 Settings.log_level，默认INFO，接受DEBUG/INFO/WARNING/ERROR/CRITICAL，非法值明确配置错误。交互控制台仅作为统一run内部组件，不保留独立console命令。run需要完整worker配置；单次status/issues/sync/recheck仍仅需数据库，issues内部交互处理按动作决定是否需要账本，普通CLI issues仍只读且只需数据库；公开resolve已删除。控制台命令复用同一能力判定，不能复制一套依赖表。

run必须在创建worker前拒绝非TTY；无人值守显式使用worker。启动器不自动migrate、搜索父目录配置或切换到Docker，不因为删去参数而改变数据和配置来源。

## 固定服务超时契约

- 范围：账本HTTP、AI HTTP和IMAP连接，统一30秒；这是客户端超时参数，不承诺整个worker阶段30秒内结束。
- 接口：移除Settings.ledger_timeout_seconds、Settings.ai_timeout_seconds和MailSettings.timeout_seconds。客户端测试依赖注入不等同用户配置。
- 输入：EBKI_LEDGER_TIMEOUT_SECONDS、EBKI_AI_TIMEOUT_SECONDS、EBKI_IMAP_TIMEOUT_SECONDS不再读取；对应.env.example、Compose和日常.env条目移除。
- 错误：旧TOML超时键提示删除，不能引导迁移到已删除变量。旧进程环境变量不参与校验或覆盖，不为此额外阻断启动。
- 正常：未配置超时的三个真实构造路径都取得30秒；基础：旧环境变量即使写成不同值也不能改变该值；错误：删除.env示例但保留另一处可配置入口。
- 验证：三个构造路径固定值、旧环境变量无效、旧TOML拒绝与安全错误、Compose映射移除。
- 正确：单一常量驱动运行超时；错误：Settings、Compose和客户端各保留一份可编辑默认值。
