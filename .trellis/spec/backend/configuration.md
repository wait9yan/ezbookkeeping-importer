# 配置入口与命令依赖

2026-09-20 用户授权统一配置来源。应用只读取业务 TOML 和进程环境，不自动搜索 `.env`，不再按秘密/非秘密拆分同一服务配置。

## 来源契约

- `config.toml`：业务策略、还款历史映射、商户规则、分类模式、时区；`mail.source_id` 为稳定业务身份。
- 进程环境：数据库；账本地址/Token/超时；AI 地址/模型/Key/超时；IMAP 主机/端口/用户名/授权码/超时；原件/报告/日志目录及轮转参数。
- `.env.example` 列出环境变量和默认值；本地使用 `uv run --env-file .env ebki --config config.toml ...` 显式加载。已有进程环境优先。应用不引入第二个 dotenv 加载器。
- Compose 显式映射同一组变量，但不能用 `${TOKEN:?}` 预先要求所有凭据，否则数据库维护命令无法执行。Docker 网络是部署层必需条件，独立于应用的命令依赖。
- 移入环境变量的旧 TOML 键必须报出字段名与对应环境变量，不能静默覆盖、丢弃或按环境缺失回退到 TOML。

## 命令能力

配置校验与依赖组装共享同一份命令能力判定，不能分别维护两套命令列表。

| 命令 | 能力要求 |
| --- | --- |
| migrate | 数据库与显式数据库初始化能力；仅缺库时经同实例维护库创建目标 |
| status、issues、sync、console | 数据库；不构造不使用的外部客户端和文件目录；console只读取日志路径，各业务命令独立组装依赖 |
| resolve 本地决定 | 数据库 |
| resolve link、带 account-id 的账户修正、restore-audit | 数据库与账本 |
| worker | 数据库、账本、IMAP、流水线与存储；AI 模式额外要求 AI 服务 |
| doctor | 检查完整 worker 连接配置，实际探测仍只包括已有数据库和账本读取；不声称 IMAP/AI 已接通 |

## 验证与错误

必需项缺失和非法输入在资源创建前失败。错误只能报告字段名、变量名和安全原因，不输出输入值、DSN、Token 或含凭据 URL。rules_only 不要求 AI；仍要拒绝已经提供但语法非法的环境值。URL、端口、有限正超时及轮转范围通过统一配置边界校验，超时必须传入真正的客户端。

正确：迁移只提供 `EBKI_DATABASE_URL`，业务 TOML 合法即可；worker 缺模型配置时在启动阶段一次列出缺项。

错误：环境缺失时悄悄使用 TOML 的旧 Key；每笔交易处理到 AI 时才发现启动配置不完整；在构造数据库之后才检查邮箱密码。

## 路径与部署

本地运行目录默认相对工作目录的 `data/email`、`data/reports`、`data/logs`，容器默认 `/app/data/*` 并对应宿主机 `./data/*` 挂载。用户通过 `EBKI_*_DIR` 覆盖 Compose 路径时必须提供绝对路径，同一值用于宿主机源、容器目标和应用环境。秘密不打包；sdist 包含 `.env.example`，不包含 `.env` 或运行配置。

## 必需回归

环境变量实际生效；业务规则保留；旧键明确迁移；必需项和非法值不泄露；按命令依赖矩阵；rules_only 无 AI；超时透传；资源构造失败清理；实际 uv 加载合成 `.env`；Compose 空凭据配置渲染；默认与覆盖路径挂载一致。

普通消费不再接受 `accounts` 配置，改从 ezBookkeeping 账户描述解析卡号，再按邮件原币唯一匹配。新流程不用汇率，不保留汇率时效配置或专用迁移检查。旧账户映射必须明确提示迁移，不能与远端描述共同构成两个映射来源；契约见 [卡号匹配与原币入账](account-matching.md)。

## 2026-09-28 自动运行与扫描配置

worker 默认自动写入通过来源、分类、账户及查重校验的任务，退款直接按负支出处理。删除 writes_enabled、refund_ownership_confirmed 及其附属 historical_boundary_reviewed；旧键必须明确提示删除，不静默迁移。暂停使用停止 worker，恢复先停止 worker 并执行 restore-audit。

IMAP 为唯一正式采集渠道，QQ 仅为服务商与专用认证策略；删除 import-eml 命令及其能力分支，原件文件格式仍为 .eml。首次常规同步全量，后续 UID 增量，跨日按 mail.rescan_days 回扫（严格非负整数，默认7，0禁用）。UIDVALIDITY变化重新全量，有界手工补扫不改变常规游标。

source_policy 与 trusted_authserv_id 已删除，旧键明确迁移报错。来源认证按 mail.host 自动选择已实现的邮箱适配；当前内置 imap.qq.com，未知主机不继承 QQ 信任。人工 accept-source 仅用于具体来源项的异常处理，记录理由和时间，不改写 requires_acceptance 认证结论；同一业务来源下其他可信来源可驱动原件继续处理。

## 日志级别与控制台

`EBKI_LOG_LEVEL` 对应 Settings.log_level，默认INFO，接受DEBUG/INFO/WARNING/ERROR/CRITICAL，非法值明确配置错误。控制台入口只装配交互环境；status/issues/sync仍仅需数据库，resolve按现有动作决定是否需要账本，不因打开控制台要求邮箱或模型凭据。控制台命令复用同一能力判定，不能复制一套依赖表。
