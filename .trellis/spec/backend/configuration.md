# 配置入口与命令依赖

2026-09-20 用户授权统一配置来源。应用只读取 TOML 和进程环境，不自动搜索 `.env`，不再按秘密/非秘密拆分同一服务配置。

## 来源契约

- `data/config.toml`：业务策略、还款历史映射、商户规则、分类模式、时区、日志级别；`mail.source_id` 为稳定业务身份。
- 进程环境：数据库；账本地址/Token；AI 地址/模型/Key；IMAP 主机/端口/用户名/授权码。
- `.env.example` 列出环境变量和默认值；本地`./run`定位项目根目录并委托uv显式加载根.env，零参数选择`ebki run`、有参数透传CLI。原始`uv run --env-file .env ebki ...`继续可用，CLI统一默认data/config.toml。已有进程环境优先，应用不引入第二个dotenv加载器。
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

本地运行目录固定为相对工作目录的 `data/email`、`data/reports`、`data/logs`，容器工作目录为 `/app`，通过单个 `./data:/app/data` bind 挂载保留同名子目录。目录及轮转参数不接受 TOML 或环境覆盖；TOML 旧字段明确拒绝，内部 Settings 仍可供测试注入。日志固定按 10 MiB 轮转，保留 5 个归档。网络名直接配置在 Compose，用户默认继承 Dockerfile 的 `10001:10001`，需要时直接设置 Compose 的 `user`；宿主机挂载目录需匹配写权限。秘密不打包；sdist 包含 `.env.example`，不包含 `.env` 或运行配置。

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


## Docker 镜像发布契约（2026-09-29）

### 1. 范围与触发
Dockerfile、Compose、CI 或发行包变化均需验证构建到部署的完整链路，不改变现有命令能力或账务逻辑。

### 2. 命令签名
- 生产：`docker compose pull importer`，显式 `run --rm importer migrate/doctor` 后 `up -d --no-build`。
- 开发：显式 `-f compose.yaml -f compose.build.yaml` 构建本地镜像。
- 验证：`python3 scripts/verify-image.py IMAGE --platform linux/amd64`（或 arm64）。
- 发布校验：`python3 scripts/check-release.py vX.Y.Z --image ghcr.io/OWNER/IMAGE`。

### 3. 数据与环境契约
`EBKI_IMAGE` 仅由 Compose 读取，空值采用 latest，支持指定版本或完整 digest；应用不读取它。生产无 build，开发覆盖不拉生产镜像。工作目录 /app、UID/GID 10001:10001 不变；统一 ./data:/app/data bind，email/reports/logs 子目录路径不变。Compose仅用简写绑定 `./data:/app/data`，配置随data目录可写；首次创建data并将config.example.toml复制为data/config.toml，配置不自动生成。CLI唯一默认data/config.toml，Docker ENTRYPOINT仅ebki并通过WORKDIR=/app复用该默认；run保持切换项目根目录和参数透传，显式--config覆盖。Compose删除init/stdin_open/tty/stop_grace_period，使用Docker默认停止期限，未决写入在下次启动核实。builder 保留 migrations 符号链接目标，runtime 只依赖安装环境；安装后的 schema.sql 必须等于权威 SQL。

### 4. 校验与错误矩阵
标签格式或 pyproject 版本不符→发布失败；正式 tag 存在→拒绝覆盖；registry 仅明确404代表不存在，403/网络错误不能当作不存在。首次先推唯一构建tag创建package，再检查正式版本tag。latest 是允许更新的正式发布别名，不参与版本不存在校验；仅正式版本发布更新 latest。临时 PG 必须等 TCP 就绪；合成配置必须含 timezone 和 mail.source_id。镜像通过两种架构验证后原样传入发布job，不重建。

### 5. 正常、基础与错误案例
正常：同一提交双架构验证后发布不可覆盖的版本标签，同步更新 latest；仓库 Compose 默认 latest，显式 EBKI_IMAGE 可选版本或 digest；Release 自动生成版本说明，不附部署包。基础：migrate 仅使用合成数据库连接运行。错误：使用 worker --once 做探活、把 doctor 视为活性检查、同名版本覆盖或将增量迁移能力归于当前 migrate。

### 6. 必需验证
tests/unit/test_release_check.py 覆盖版本、已存在、404、权限及网络失败；test_image_smoke_config.py 通过真实配置边界验证合成配置。镜像 smoke 验证非 root、SQL、时区、生产依赖、统一data卷下配置及三个子目录重建保留、空宿主data bind写入；隔离PG两次migrate/status必须经镜像真实默认入口且不传--config，避免掩盖默认路径漂移。所有后端测试硬超时60秒；CI工作流需 actionlint。

### 7. 错误与正确做法
错误：源码镜像测试通过后重新构建发布镜像；镜像已发布但Release失败就删镜像重发。
正确：传递已测试镜像artifact；独立 Release job 在镜像成功且 Release 尚未创建时可单独重跑；部署示例从对应版本标签的仓库获取，不生成部署压缩包、校验文件或附件。已有 Release 的自动核验与幂等续跑尚未实现，手工恢复见 operations 文档。


### 8. 当前实施范围与后续优化

2026-09-30 的 CI 调整只移除部署包生成、SHA-256 校验文件、deployment artifact 和 Release 附件流程；保留传递实际已验证镜像的 image artifact 及现有镜像发布逻辑。GitHub Release 当前通过 `gh release create --verify-tag --generate-notes` 自动生成版本说明，不包含部署附件。部署示例保留在仓库，按版本标签获取并用 EBKI_IMAGE 选镜像。

完整 PostgreSQL 集成测试门禁、同源码/同镜像核验后的安全续跑、已有 Release 标签与提交核验，以及跨版本串行更新 latest 并防止旧版覆盖均属于后续优化，尚未实施；不纳入此次“仅移除部署包”的完成条件。当前正式版本已存在时拒绝覆盖，不能将此描述成完整幂等续跑或 latest 防回退。


## data/config.toml 默认路径迁移（2026-09-29）

### 1. 范围
配置位置统一适用于本地CLI、./run、交互run和Docker；不改变TOML字段或.env来源。

### 2. 命令签名
`ebki status`默认读取工作目录下data/config.toml；`ebki --config FILE status`只读取显式文件。./run将工作目录切到项目根目录；Docker通过WORKDIR=/app复用同一CLI默认。

### 3. 契约
Compose只挂./data:/app/data；实际配置为data/config.toml，随目录可写。示例config.example.toml和.env继续位于根目录。/data/已有Git忽略和Docker白名单保护；保留旧根config忽略条目，不能误提交迁移前遗留文件。已有实际配置移动时必须保留内容及权限，目标存在不覆盖。

### 4. 错误矩阵
缺data/config→既有ConfigurationError；仅旧根config存在→仍失败、不回退、不创建新配置；显式路径可读→独立于默认配置成功；显式缺失→错误、不回退默认。未知字段和环境缺项行为不变。

### 5. 正常/基础/错误案例
正常：新配置读取且根目录冲突/非法配置被忽略。基础：只提供显式配置即可运行维护命令。错误：修改Docker参数却保留本地默认根config，或初始化自动复制示例覆盖用户配置。

### 6. 验证
配置回归经parse_command+真实load_settings覆盖默认选择、显式优先与缺失失败；启动器原参数透传/根目录定位回归保留。双架构镜像必须读取data卷持久化合成配置，用默认入口执行维护命令。发行包不得含data/config.toml。

### 7. 对照
错误：Dockerfile硬编码另一个--config默认值或程序搜索两个位置。
正确：CLI单一定义默认；Docker仅执行ebki，用户显式--config覆盖。旧配置迁移作为一次性运维步骤，程序不隐藏迁移缺失。
