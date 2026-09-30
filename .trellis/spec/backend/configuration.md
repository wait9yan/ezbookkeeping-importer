# 配置入口与命令依赖

配置加载器只读取业务 TOML 和进程环境；所有 CLI 入口在读取前初始化缺失的默认业务配置。不自动搜索 `.env`，每项服务连接仍只有环境变量一个来源。

## 来源契约

- `data/config.toml`：业务策略、还款历史映射、商户规则、分类模式、时区、日志级别；`mail.source_id` 为稳定业务身份。
- 进程环境：数据库；账本地址/Token；AI 地址/模型/Key；IMAP 主机/端口/用户名/授权码。
- `.env.example` 列出环境变量和默认值；本地`./run`定位项目根目录并委托uv显式加载根.env，零参数选择`ebki run`、有参数透传CLI。原始`uv run --env-file .env ebki ...`继续可用，CLI统一默认data/config.toml。已有进程环境优先，应用不引入第二个dotenv加载器。
- Compose 显式映射同一组变量，但不能用 `${TOKEN:?}` 预先要求所有凭据，否则数据库维护命令无法执行。Docker 网络是部署层必需条件，独立于应用的命令依赖。
- TOML在环境注入前按当前业务模型严格校验，只接受业务字段；未知字段统一报告 `unknown TOML field`，不报告输入值。服务连接字段仅由环境提供，不在TOML与环境之间设置覆盖优先级。

## 命令能力

配置校验与依赖组装共享同一份命令能力判定，不能分别维护两套命令列表。

| 命令 | 能力要求 |
| --- | --- |
| migrate | 数据库与显式数据库初始化能力；仅缺库时经同实例维护库创建目标 |
| status、issues、sync、recheck | 数据库；不构造不使用的外部客户端和文件目录；各业务命令独立组装依赖。recheck只安排一次复查，真正查重和写入由worker执行 |
| issues show、issues resolve 本地动作 | 数据库 |
| issues candidates、resolve 关联/账户修正；restore-audit | 数据库与账本 |
| run | 数据库、账本、IMAP、流水线与存储；AI 模式额外要求 AI 服务；run在同一进程构造Runtime并执行worker，不创建控制台或子worker |
| doctor | 检查完整 worker 连接配置，实际探测仍只包括已有数据库和账本读取；不声称 IMAP/AI 已接通 |

## 验证与错误

必需项缺失和非法输入在资源创建前失败。错误只能报告字段名、变量名和安全原因，不输出输入值、DSN、Token 或含凭据 URL。rules_only 不要求 AI；仍要拒绝已经提供但语法非法的环境值。URL、端口及日志级别通过统一配置边界校验。账本、AI与IMAP客户端超时固定30秒并传入实际客户端，不提供环境变量或TOML覆盖；数据库连接超时仍按原契约。

正确：迁移只提供 `EBKI_DATABASE_URL`，业务 TOML 合法即可；worker 缺模型配置时在启动阶段一次列出缺项。

错误：把服务连接或内部运行参数当成业务 TOML 字段；每笔交易处理到 AI 时才发现启动配置不完整；在构造数据库之后才检查邮箱密码。

## 路径与部署

本地运行目录固定为相对工作目录的 `data/email`、`data/reports`、`data/logs`，容器工作目录为 `/app`，通过单个 `./data:/app/data` bind 挂载保留同名子目录。目录及轮转参数不接受 TOML 或环境覆盖；业务 TOML 模型不包含内部运行参数，内部 Settings 仍可供测试注入。日志固定按 10 MiB 轮转，保留 5 个归档。网络名直接配置在 Compose，用户默认继承 Dockerfile 的 `10001:10001`，需要时直接设置 Compose 的 `user`；宿主机挂载目录需匹配写权限。秘密不打包；sdist 包含 `.env.example`，不包含 `.env` 或运行配置。

## 必需回归

环境变量实际生效；业务规则保留；顶层及嵌套未知字段拒绝且不泄露值；服务连接和固定运行参数不能从TOML注入；按命令依赖矩阵；rules_only 无 AI；固定30秒超时实际传递；资源构造失败清理；实际 uv 加载合成 `.env`；Compose 空凭据配置渲染；固定路径挂载一致；日志级别仅从 TOML 读取。

消费账户从 ezBookkeeping 账户描述解析卡号，再按邮件原币唯一匹配。首次入账不用汇率；契约见 [卡号匹配与原币入账](account-matching.md)。

## 2026-09-28 自动运行与扫描配置

run 自动写入通过来源、分类、账户及查重校验的任务，退款直接按负支出处理。暂停执行停止服务；从备份恢复时先保持服务停止并执行 restore-audit。

IMAP 为唯一正式采集渠道，QQ 仅为服务商与专用认证策略；删除 import-eml 命令及其能力分支，原件文件格式仍为 .eml。首次常规同步全量，后续 UID 增量，跨日按 mail.rescan_days 回扫（严格非负整数，默认7，0禁用）。UIDVALIDITY变化重新全量，有界手工补扫不改变常规游标。

来源认证按 mail.host 自动选择已实现的邮箱适配；当前内置 imap.qq.com，未知主机不继承 QQ 信任。人工 accept-source 仅用于具体来源项的异常处理，记录理由和时间，不改写 requires_acceptance 认证结论；同一业务来源下其他可信来源可驱动原件继续处理。

## 日志级别与单次命令

TOML顶层log_level默认INFO，接受DEBUG/INFO/WARNING/ERROR/CRITICAL，非法值明确配置错误。run不要求TTY、不读取stdin，非TTY输出结构化运行事件；单次维护默认JSON，可显式--format text。issues show/candidates/resolve及快照限定recheck复用应用层规则，详见[单次维护命令](cli-maintenance.md)。

公开worker/worker --once、console及交互exit入口移除。Runtime默认完整运行能力为run，启动器按显式参数和唯一默认路径读取配置，不自动migrate或切换到Docker。维护进程退出不停止run。

## 固定服务超时契约

- 账本HTTP、AI HTTP和IMAP连接统一30秒；这是客户端超时参数，不承诺整个处理阶段30秒内结束。
- 单一常量驱动实际客户端构造，不提供TOML或环境配置入口。客户端的测试依赖注入不等同用户配置。
- 必需验证：三个真实客户端构造路径均取得30秒，业务配置仅含当前公开字段。
- 正确：复用一个运行常量；错误：Settings、Compose和客户端各保留一份可编辑默认值。

## Docker 镜像发布契约（2026-09-29）

### 1. 范围与触发
Dockerfile、Compose、CI 或发行包变化均需验证构建到部署的完整链路，不改变现有命令能力或账务逻辑。

### 2. 命令签名
- 生产：`docker compose pull importer`，显式 `run --rm importer migrate/doctor` 后 `up -d --no-build`。
- 开发：显式 `-f compose.yaml -f compose.build.yaml` 构建本地镜像。
- 验证：`python3 scripts/verify-image.py IMAGE --platform linux/amd64`（或 arm64）。
- 发布校验：`python3 scripts/check-release.py vX.Y.Z --image ghcr.io/OWNER/IMAGE`。

### 3. 数据与环境契约
`EBKI_IMAGE` 仅由 Compose 读取，空值采用 latest，支持指定版本或完整 digest；应用不读取它。生产无 build，开发覆盖不拉生产镜像。工作目录 /app、UID/GID 10001:10001 不变；统一 ./data:/app/data bind，email/reports/logs 子目录路径不变。Compose仅用简写绑定 `./data:/app/data`，配置随data目录可写；首次准备可写data目录，所有CLI入口自动创建缺失的默认data/config.toml，具体契约见“配置文件位置”。CLI唯一默认data/config.toml，Docker ENTRYPOINT仅ebki并通过WORKDIR=/app复用该默认；run保持切换项目根目录和参数透传，显式--config覆盖。Compose删除init/stdin_open/tty/stop_grace_period，使用Docker默认停止期限，未决写入在下次启动核实。builder 保留 migrations 符号链接目标，runtime 只依赖安装环境；安装后的 schema.sql 必须等于权威 SQL。

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


## 配置文件位置与自动初始化（2026-09-30）

### 1. 范围与触发

本地源码、`./run`、直接 `ebki`、安装包和 Docker 共用 CLI 初始化。首次正式命令在默认文件缺失时创建实际 `config.toml` 并继续原命令，无需用户准备 example 文件。配置生成不会隐式执行数据库迁移。

### 2. 签名

- CLI 唯一默认路径为 `DEFAULT_CONFIG_PATH = "data/config.toml"`。
- `config_initialization.initialize_default_config(path: Path) -> None` 负责创建，`load_settings(path, command=...)` 负责读取及校验。
- `ebki status` 和 `ebki run` 初始化默认文件；`ebki --config FILE status` 只读取显式文件。
- Docker `ENTRYPOINT ["ebki"]`、`CMD ["run"]`、`WORKDIR /app`，复用普通 CLI，不保留镜像专属初始化入口或另一份模板。

### 3. 来源与发布契约

`src/ezbookkeeping_importer/config.toml` 是唯一非凭据默认资源，用 `importlib.resources` 从安装包读取，随 wheel、sdist 和镜像提供；不依赖源码目录或根 example 文件。默认业务值包含上海时区、12:00 日期时间约定、AI 分类、`qq-primary`、7 天回扫及未确认还款归属；连接信息和凭据仍只来自环境。

CLI 参数解析成功并注册信号后，仅未显式指定 `--config` 时初始化默认文件。已有目标通过 lstat 判断并保留，包括空文件、目录、符号链接和非法 TOML，随后由既有加载器校验。缺失目标在同目录写完整临时文件，flush/fsync 后用无覆盖的原子操作发布；并发已有目标保留对方文件。其他 I/O 错误明确失败，正常及异常路径清理临时文件。

`./run` 定位项目根目录；安装后 CLI 使用调用方工作目录。Compose 挂载 `./data:/app/data`，以 `10001:10001` 创建并持久化配置，宿主机必须提供写权限，不自动 chown 或提权。实际配置和 `.env` 仍受忽略及构建白名单保护。配置自动生成发生于服务连接检查之前，缺凭据时会留下已生成的合法业务文件。

### 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 默认文件缺失且父目录可写 | 创建完整配置后继续命令 |
| 已有合法文件（含只读文件） | 原样读取，不要求目录可写 |
| 已有空文件或非法 TOML | 按真实配置边界失败，不替换 |
| 父目录不可写、存储或模板读取失败 | 安全 `ConfigurationError`，不打印输入或凭据 |
| 显式 `--config` 文件缺失 | 读取失败，不创建指定或默认文件 |
| 帮助或参数解析错误 | 退出，不创建配置 |
| 并发初始化 | 首个发布者胜出，不覆盖完整目标 |

### 5. 正常、基础与错误案例

正常：首次 `./run migrate` 自动生成业务配置，成功初始化目标库，再编辑个性化规则并运行。基础：未配置服务连接执行正式命令，配置仍生成，但命令明确报告必需环境变量缺失。错误：把空或非法已有文件视为缺失并替换，或在 `--help` 时要求数据目录写权限。

### 6. 必需验证

单元覆盖真实默认选择、首次生成、重复及并发无覆盖、非法已有文件保留、权限和 I/O 失败、显式路径严格失败及帮助无写入。启动器保留参数透传和根目录定位，同时验证实际 CLI 初始化。安装后的 wheel/sdist 必须可读默认资源，真实安装 CLI 在空目录生成配置；构建产物不包含实际运行配置或凭据。

双架构镜像从空可写 data 挂载、通过真实默认入口执行 `migrate`、重复迁移及 `status`，不得先写合成配置绕过初始化；读取生成内容与包资源一致。后续生命周期可提供合成业务定制，但不得替代首次生成验收。非 root、PID 1、无 TTY 及停止恢复契约保持原测试强度。

### 7. 正确与错误做法

正确：所有入口调用同一个初始化函数，从包资源创建实际配置，已有文件交由加载器失败或读取。错误：镜像和本地各维护一套模板/初始化逻辑，构建阶段创建文件后假定 bind 挂载仍能看到，或初始化时覆盖用户配置。

## 单进程运行契约（2026-09-30）

### 1. 范围

run是唯一持续运行入口。生命周期不依赖交互控制台，单次命令直接复用应用用例和数据库。

### 2. 签名

`ebki run`、根./run无参数及Docker CMD=["run"]均进入run_service(config_path)->int。Docker ENTRYPOINT=["ebki"]，Compose不重复command。

### 3. 契约

在Runtime初始化前注册SIGTERM/SIGINT，单进程持有数据库会话锁；不创建multiprocessing子worker。Compose不配置init/stdin_open/tty/stop_grace_period，保留用户选定restart=unless-stopped。普通exec -T维护不分配TTY；单次命令关闭自身Runtime，不能停止后台。

### 4. 错误矩阵

无TTY/关闭stdin→持续运行；锁冲突→明确失败并关闭自身连接；停止信号→逐项合作停止；默认期限耗尽→Docker强杀；重启→恢复同步并核实UNKNOWN，不直接重发。compose stop保持停止，up -d恢复，异常退出遵循unless-stopped。

### 5. 案例

正常：up -d后exec -T status查询，命令退出后台仍持锁。基础：migrate不要求完整外部凭据。错误：将UNKNOWN查无结果自动重新排队，或把命令中断视为全部回滚。

### 6. 必需测试

初始化前信号、PID1、无TTY/输入关闭、唯一进程与锁冲突、单次CLI独立、合作停止、真实SIGKILL与真实HTTP写入次数、双架构默认镜像。不覆盖CMD或挂宿主src冒充发行包验收。

### 7. 错误与正确

错误：忽略EOF后保留隐藏控制台，添加应用内重启或Docker socket。正确：删除UI与子进程管理，保留明确停止状态和持久化恢复协议。
