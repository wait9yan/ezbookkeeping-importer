# 配置与运维参考

[返回 README](../README.md)

本文保留完整的账务行为、配置、维护和恢复说明。首次使用请先阅读 README 的快速开始。本地开发命令在项目根目录执行；Docker 运维命令在包含 compose.yaml、.env 和 data/config.toml 的部署目录执行。

## 账务行为

- 首次扫描全部可读取 IMAP 文件夹，每批最多读取 50 封邮件头，仅银行候选下载全文，其他邮件持久标记为 skipped；按 UID 保存下载进度；历史消费纳入补记。重启立即恢复，上海时间 17:00 至午夜每 10 分钟检查，其余时段每小时检查。
- 分类采用用户规则与模型；若明确选择 `classification_mode="rules_only"` 则只运行规则。缺少必需模型配置会形成异常；有效的无法匹配结果使用已有的「其他杂项 → 待分类」二级支出分类。接口失败、无效 JSON、未知分类 ID 均为异常，不冒充无法匹配。该分类路径必须唯一且父子分类均可用，应用不自动创建分类。
- 消费账户由 ezBookkeeping 账户描述中的银行卡号与邮件原币自动匹配，必须唯一命中可见、可记账的同币账户。邮件完整卡号精确匹配，仅尾号时匹配末四位；不会默认选首个账户或跨币种入账。
- 日报退款为同币账户中的负支出；还款为付款账户到信用卡的转账。还款跨渠道归属需确认；还款邮件没有卡号，仍单独配置两端账户及生效日期。
- 新外币消费按原币金额直接记入对应币种账户，例如 USD 消费记入 USD 子账户，不读取汇率。月账单同币结算核对银行结算金额；USD 交易唯一匹配可信人民币结算后，通过 `settle_currency` 保留原远端 ID，同时改为同卡 CNY 账户及银行实际金额，保留当前分类、备注、时间、标签和图片。候选歧义、账户缺失或目标被改动时保留问题，不猜测关联、不删除重建。历史已保存的人民币暂估决定继续沿用冻结金额与旧结算路径；日期被修改仍报告 `target_changed`，不会改回日期。
- 写入超时或进程中断进入 `unknown`，通过来源标记查询并回读核实，不直接重发 POST。月账单缺日报证据不会自动补建消费；远端删除的交易不会被自动复活。

核对由新来源、分类、人工处理及写入结果变化触发；没有变化时每小时复核历史，远端查询失败后 10 分钟重试。闲置的 30 秒 worker 轮询不再重复请求全部远端记录或重写核对报告。每份月账单在 `bank_report` 保存自己的核对调度和发布版本；完整结果集合原子发布，半轮失败保留上一完整集合，重启继续重试。

显式重试尚未关联的创建操作时，会按最新规则／模型重新分类，保留已冻结的金额、汇率、时间、来源标记和人工账户修正；`unknown` 或已领取操作仍只能核实结果。

## 配置与本地开发

需要独立的 importer PostgreSQL 数据库，以及已有的 ezBookkeeping 服务；可以复用 PostgreSQL 实例，不要使用 ezBookkeeping 业务数据库保存 importer 状态。

配置只有两个来源：服务连接由进程环境提供，业务规则由 TOML 提供。环境变量及默认值见 [.env.example](../.env.example)，业务字段及注释见[内置默认配置](../src/ezbookkeeping_importer/config.toml)。未知 TOML 字段报 `unknown TOML field`，不会被忽略或改写。

### 配置文件位置

所有 CLI 入口默认使用工作目录下的 `data/config.toml`；`./run` 定位项目根目录，安装后的 `ebki` 使用调用方工作目录，Docker 工作目录为 `/app`。首次正式命令创建缺失的默认配置并继续执行，本地、安装包及容器行为一致；生成内容不含服务凭据。

已有文件始终保留，包括空文件和非法 TOML，随后按配置规则校验。`--help` 不创建配置。使用 `--config FILE` 可选择已准备的文件，指定文件缺失时明确报错，不自动生成或回退，例如 `./run --config /path/to/config.toml run`。

默认配置使用 AI 分类、上海时区、12:00 日期默认时间、`qq-primary` 邮箱身份及 7 天回扫窗口。首次迁移后可编辑商户规则、还款映射和分类模式；`classification_mode = "rules_only"` 不要求模型服务，也完全关闭模型调用。保持 `mail.source_id` 稳定。

### 本地命令与服务连接

在项目根目录执行，已有配置请保留：

```sh
cp .env.example .env
uv sync --frozen
# 填写 .env 的数据库连接。
./run migrate
# 编辑生成的 data/config.toml，再补齐启动所需的服务连接。
./run doctor
./run
```

`./run` 通过 uv 显式加载项目 `.env`，无参数选择 run，有参数透传给 `ebki`。原始命令为 `uv run --env-file .env ebki run`；普通 `uv run`、已安装的 `ebki` 或激活虚拟环境均不会自动加载 `.env`。应用不另设 dotenv 加载器，已有进程环境变量优先。项目虚拟环境中的命令位于 `.venv/bin/ebki`，未加入 PATH 时使用 `./run` 或上述 uv 命令。

服务连接只写入 `.env` 或进程环境，不写 TOML。`.env` 不进入源码发行包或镜像；`.env.example` 是可分享的空值模板。

| 环境变量 | 用途与默认值 |
| --- | --- |
| `EBKI_DATABASE_URL` | importer PostgreSQL 连接串；所有实际维护命令必需 |
| `EBKI_LEDGER_URL` | ezBookkeeping 站点根地址，可含部署子路径，不加 `/api/v1` |
| `EBKI_LEDGER_TOKEN` | ezBookkeeping API Token |
| `EBKI_AI_URL`、`EBKI_AI_MODEL`、`EBKI_AI_TOKEN` | 模型地址、模型名、API Key；地址通常以 `/v1` 结尾 |
| `EBKI_IMAP_HOST`、`EBKI_IMAP_PORT` | 默认 `imap.qq.com`、`993` |
| `EBKI_IMAP_USERNAME`、`EBKI_IMAP_PASSWORD` | IMAP 登录名及凭据；QQ 使用邮箱地址和 IMAP 授权码 |

账本、AI 和 IMAP 客户端超时固定为 30 秒，不提供环境变量或 TOML 配置。端口和日志级别在使用前校验，缺少必需项时列出变量名，不回显秘密。依赖按命令检查：

| 命令 | 必需服务配置 |
| --- | --- |
| `migrate/status/issues/sync/recheck` | 数据库；recheck 只安排复查 |
| `issues resolve` 的接纳、忽略、普通重试、确认新建 | 数据库；实际执行交后台 |
| `issues candidates`、`issues resolve` 的关联、账户修正，以及 `restore-audit` | 数据库与账本 |
| `run`、`doctor` | 数据库、账本、邮箱；AI 模式额外要求模型地址、名称及 Key |

`EBKI_AI_URL` 是模型 API 基址，应用追加 `/chat/completions`。地址若返回 HTML 首页，即使 HTTP 200 也不能通过分类校验，应核对真实 API 路径。
日志级别在 TOML 顶层设置 `log_level = "INFO"`，支持 `DEBUG/INFO/WARNING/ERROR/CRITICAL`，修改后重启。

### 数据库初始化与版本迁移

`run` 与 `migrate` 使用同一迁移链，自动数据库准备自 [v0.2.2](https://github.com/wait9yan/ezbookkeeping-importer/releases/tag/v0.2.2) 引入。其他维护命令不建库或迁移。开发者添加结构变更见[数据库结构变更与发布](database-migrations.md)。

只在明确缺库时，尝试创建 `EBKI_DATABASE_URL` 显式指定的目标库；密码、DNS、网络等失败不触发创建。新库从初始版本执行完整链，受支持旧库只执行未应用版本，完成校验后才开始业务恢复与导入。应用不创建角色或授予权限。

数据库业务表为 `email_sync_checkpoint`、`email_source_item`、`email`、`bank_report`、`bank_transactions`、`background_task`、`ledger_write_attempt`、`bank_statement_reconciliation`、`schema_version`，表与字段均有中文注释。

已发布脚本不得改写。每个版本的 DDL、必要数据转换和版本/校验和记录在同一事务提交；一版失败只回滚本版，前序已提交版本保留，再次启动从最后成功版本继续。脚本改写、历史不完整、结构漂移或数据库比程序新时明确失败，不推测修复、删数据或自动降级。已有最新结构的校验只读，不创建临时 schema。

权限按阶段要求：缺库需同实例 postgres 维护库 CONNECT 与 CREATEDB；初始化需目标 schema 建表权限；升级既有表通常需对象所有者或迁移角色。容器 root 不能替代 PostgreSQL 权限。最新库无须持续 DDL 权限。已发布 v1 的迁移历史缺少脚本校验和，严格验证基线后需一次元数据升级；普通账号无修改权限时，由管理员先执行同一 migrate，再以普通账号运行。

run 的准备过程和独立 migrate 都与业务 worker 排他。升级前停止旧 importer 和旧版本维护操作，再配对备份；不支持混合版本滚动运行。数据库暂时未就绪会明确退出，Docker 按 restart 策略重试。使用外部网络时，不要添加并不存在的 postgres 服务依赖。

`doctor` 校验完整启动配置，并只读检查迁移历史、结构就绪及账本读取连通性；空库、待升级或不兼容结构明确失败。它不迁移、不连接 IMAP、不调用模型、不测试实际入账，也不检查 worker 活性，不能用作健康检查。

大规模回填、非事务操作和破坏性变更需版本专项维护方案。镜像回退条件见[镜像升级与回退](#镜像升级与回退)。

### 业务初始化

核对 ezBookkeeping 账户描述及 `data/config.toml` 中的业务设置：

1. 在 ezBookkeeping 可记账子账户的描述中填写银行卡号（12–19 位，可含空格或连字符分组），并设置正确币种。按卡号和币种唯一匹配。同尾号同币种多个候选、或缺少同币账户会明确报错；历史换卡记录仍需核实，不按今天的账户名称猜归属。
2. 核对历史消费与初始负债、既有交易和其他导入渠道的重复边界。退款自动按负支出处理，仍执行查重；还款需要配置账户映射和渠道归属。
3. 还款两端人民币账户、二级转账分类，以及仅日期通知的 `date_only_time` 记账约定。
4. 当前支持 CNY 与 USD，美元需要对应 USD 子账户；同一张卡只有 USD 子账户时，其 CNY 消费不能自动写入 USD 账户。首次入账不进行汇率换算。
5. 保持 `mail.source_id` 稳定。IMAP 来源认证按连接主机自动选择：当前 `imap.qq.com` 使用 QQ 收件链及 SPF/DKIM/DMARC 结果检查，通过后自动接纳；不自行执行 DKIM 公钥验签。来源缺失、失败或无法判断时进入异常处理。

启动 run 后台服务后自动采集、解析、分类、匹配账户、查重并写入通过校验的交易，包括已有待写任务。缺少配置、匹配歧义或结果不明的记录保留为异常。

日常自动采集使用 IMAP，主机、端口和凭据由环境变量指定，无人工接纳运行模式或来源策略开关。目前内置 QQ 来源认证；其他主机仍能连接采集，但尚未适配的认证会明确产生来源异常，不能将 QQ 邮件头直接当成其他邮箱的可信依据。已知银行主题带 `Fw:`、`Fwd:` 或 `转发：` 前缀时仍保存原件并尝试解析，但转发者认证通过不代表原银行邮件可信。来源认证和人工接纳绑定具体 IMAP 来源项。同一业务来源、同一原件只要存在可信或已接纳来源项即可继续处理，不改写其他位置的认证结论。

首次常规同步全量扫描所有可选邮箱文件夹，之后按 UID 增量扫描。首次范围上界固定，`status` 的历史完成展示由该范围内来源项派生；增量不扩大首次范围，UIDVALIDITY 改变后建立新的首次范围。采集完成不等于解析或入账完成。`[mail] rescan_days = 7` 指定跨日回扫窗口，设为 `0` 禁用回扫；UIDVALIDITY 改变时重新全量扫描。手工补扫见[日志与单次维护命令](#日志与单次维护命令)。

## 启动和维护

部署示例从[目标正式版本](https://github.com/wait9yan/ezbookkeeping-importer/releases)的仓库获取，Release 提供版本说明、不附部署压缩包。生产 Compose 默认从 GHCR 拉取 `latest`（Linux AMD64/ARM64）；需要固定版本或 digest 时在 `.env` 设置完整 `EBKI_IMAGE` 引用。

Compose 只启动 importer，使用已有外部网络 `ezbookkeeping`；网络名不同时修改 `networks.ezbookkeeping.name`。PostgreSQL 和 ezBookkeeping 地址须能从容器访问，例如 `http://ezbookkeeping:8080`；容器内 `127.0.0.1` 指向 importer 自身。Compose 读取 `.env` 并显式注入服务变量，仍由应用按命令检查必需凭据。

```sh
cp .env.example .env
mkdir -p data
# 填写 .env，并准备账户和分类。
docker compose pull importer
docker compose run --rm importer migrate
# 按需编辑 data/config.toml 后再检查、启动。
docker compose run --rm importer doctor
docker compose up -d --no-build
```

默认命令为单进程 `ebki run`，业务进程保持 PID 1；Compose 继承镜像命令。查询运行中容器：

```sh
docker compose exec -T importer ebki status
docker compose exec -T importer ebki issues --format text
docker compose logs -f importer
```

### 数据目录权限

Compose 将 `./data` 挂载到 `/app/data`，运行子目录固定为 `email`、`reports`、`logs`。`0.2.1` 的 Docker 入口先以 root 准备必要权限，再通过 gosu 切换为 `10001:10001` 执行原 CLI；默认启动、新建维护容器及 exec ebki 共用入口。旧版本差异见 [v0.2.1](releases/v0.2.1.md)。

权限准备限于默认配置和运行子树；维护命令仅处理配置所需权限，run 还检查运行子树。已满足权限的对象不修改；必要修复会改变宿主机相关文件所有权或 owner 权限，不改变内容，不处理根目录其他文件，不沿符号链接修改外部位置。只读挂载上的维护命令直接降权读取，已有可读配置无需目录写权限；必要修复被只读存储或 NAS ACL 拒绝时明确失败。

如需自行管理权限，用 `id -u`、`id -g` 查看身份，并在 `services.importer` 下设置非 root `user: "实际UID:实际GID"`。入口保留该身份，部署者负责挂载读写权限。本地 CLI 不提权；覆盖 `--entrypoint` 会绕过容器权限入口，需自行选择身份。

### 源码部署

准备相同配置、外部网络和数据目录后，用构建覆盖文件：

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose -f compose.yaml -f compose.build.yaml run --rm importer migrate
# 按需编辑 data/config.toml。
docker compose -f compose.yaml -f compose.build.yaml run --rm importer doctor
docker compose -f compose.yaml -f compose.build.yaml up -d --no-build
```

源码部署的启动、恢复、重建及新建维护容器均保留 `docker compose -f compose.yaml -f compose.build.yaml` 前缀，确保选择本地镜像；查询运行中容器可用普通 `docker compose exec -T importer ebki ...`。生产 Compose 不含 build；拉取失败时检查权限和网络，不自动切换为源码构建。

### 停止与恢复

| 操作 | 结果 |
| --- | --- |
| 维护命令结束或中断 | 仅结束当前维护进程；已提交结果保留 |
| `docker compose stop importer` | 请求停止，默认十秒后仍未退出可被强杀，并保持停止 |
| `docker compose up -d` | 启动或恢复服务，核实中断的账本操作 |
| 进程意外退出 | `unless-stopped` 自动重启 |
| Docker 服务重启 | 恢复此前未被手动停止的服务 |

run 通过 SIGTERM/SIGINT 停止，不依赖 attach、TTY 或 stdin。程序在邮件批次、交易、核实任务和报告边界响应停止；网络请求仍采用 30 秒超时，不保证在 Docker 默认十秒内完成。

强杀后同步任务重新排队，已提交原件和决定保留；账本写入转 UNKNOWN，下次启动先核实，不自动重发。已登记但尚未发送的任务也可能待核实，远端查不到不足以证明未发送。报告 JSON 可滞后于数据库，临时文件可能残留，不承诺立即恢复所有派生文件。

数据库断连时 worker 明确失败退出，由 Compose 重启并重新获取排他锁；本地需重新执行 `./run`。持续暂停必须停止服务；停止、重建或回退镜像不会撤销远端交易。`doctor/status/issues/restore-audit` 不创建交易。

### 镜像升级与回退

升级先阅读目标 GitHub Release 的兼容说明，对照该版本 Compose 和配置字段更新，保留现有配置、外部网络及挂载差异。

1. 阅读目标正式版本的数据库结构说明，保留 `.env` 的 `EBKI_IMAGE` 为空并执行 `docker compose pull importer` 拉取 `latest`；需要固定目标版本时将该变量设为完整版本或 digest 引用。记录当前运行镜像的版本或 digest，供兼容性允许时回退。拉取失败时先解决问题，仍在运行的旧容器不受影响。
2. 执行 `docker compose stop importer`，确认旧 worker 已停止，且没有并发维护写入，再配对备份 importer 数据库、原始邮件和配置。
3. 对声明兼容的版本执行 `docker compose run --rm importer migrate` 与 `docker compose run --rm importer doctor`。已发布版本按对应说明操作，迁移机制见[数据库初始化与版本迁移](#数据库初始化与版本迁移)，结构漂移不可通过删库绕过。
4. 全部检查成功后，执行 `docker compose up -d --no-build` 应用更新；仅拉取不会替换运行中的容器。通过 `status`、`issues` 和日志核查恢复情况。

回退前先停止新 worker。只有旧镜像仍兼容当前数据库结构和迁移历史时，才把 `EBKI_IMAGE` 改回保存的旧版本/digest，执行拉取、诊断与重建。镜像回退不恢复数据库，也不撤销远端账目；涉及恢复备份时按[持久化、备份与恢复](#持久化备份与恢复)流程先核实远端状态。

### 镜像构建与发布

GitHub Actions 验证 PR、主分支和版本标签，仅正式版本标签触发发布。标签 `vX.Y.Z` 必须与 `pyproject.toml` 版本一致；维护者先更新版本和锁文件、评审合入，再推送标签。已发布版本不覆盖，修复使用新版本。

正式流程在 Linux AMD64/ARM64 上验收实际镜像，通过后推送 `ghcr.io/wait9yan/ezbookkeeping-importer:X.Y.Z` 并更新 latest，再创建自动生成说明的 GitHub Release。镜像记录提交标签与 OCI revision，验收产物传给发布 job，不重新构建。工作流使用 GITHUB_TOKEN，不需要生产连接或个人推送凭据。

构建和验收覆盖 CLI、依赖、时区、非 root 数据写入、数据库初始化与恢复。完整迁移链、符号链接目标及匹配的结构契约必须进入安装包，不能仅检查初始 SQL；开发者流程见[数据库结构变更与发布](database-migrations.md)。

镜像发布与 Release 创建为独立 job。镜像已发布而 Release 步骤失败时，先检查该版本是否已有 Release：

```sh
# 将 vX.Y.Z 替换为失败运行对应的版本标签。
gh release view vX.Y.Z --repo wait9yan/ezbookkeeping-importer
```

确认尚未创建时，在该次 Actions 运行选择“Re-run failed jobs”，仅补跑 Release；鉴权或网络错误不能当作不存在。已有 Release 时核对标签和说明，不重复创建。恢复时不要重推同名标签、删除正式镜像或重跑全部发布任务。

## 日志与单次维护命令

`./run`或`ebki run`持续运行并输出事件，不创建菜单、提示符或stdin读取循环。非TTY标准输出与`data/logs/worker.jsonl`均为结构化事件；本地TTY日志可用中文展示。日志级别在`data/config.toml`顶层设置，正常进度限频、空闲轮询安静。日志格式和单次命令结果格式独立，状态仍以数据库为准。

同步命令只提交请求，后台 run 才执行；返回不表示采集或入账已完成：

```sh
./run sync
./run sync --since 2026-06-01 --until 2026-06-30
```

`--since` 和 `--until` 必须同时提供，格式为 YYYY-MM-DD，包含起止两天，按 IMAP 邮件接收日期（INTERNALDATE 日期部分）筛选，不是消费发生日期。区间补扫使用独立任务，与普通同步串行，不改变首次历史范围、游标或完成状态；重复请求按同一来源去重。省略日期继续原有全历史／增量流程，不能用补扫限制自动导入范围。

普通维护命令默认JSON，可显式`--format text`输出中文表格；不根据终端状态切换业务行为。错误写stderr；0表示正常完成，1表示运行或业务失败，2表示参数错误，中断用130/143。中断不表示已提交动作被撤销；`restore-audit`和批量复查尤其可能部分完成，需要重新查询。

```sh
./run status --format text
./run issues --entity-type bank_transactions --code duplicate_candidates --format text
./run issues show --entity-type bank_transactions --entity-id 交易ID --code duplicate_candidates > selected.json
./run issues candidates --snapshot selected.json --format text
./run issues resolve --snapshot selected.json --action link --target-id 已有账单ID --reason '已核对为同一笔'
```

show输出和`issues --snapshot-out FILE`均采用`snapshot_version: 1`及`items`数组，每项包含issue、state、view。多项不自动选第一项；单项处理时保留所选项的完整数据。state仅为比较前置条件，不会被用来覆盖数据库payload；view仅用于展示。日期与金额经过统一规范化，不能修改快照绕过业务校验。候选查询包含本地冻结决定、远端候选、账户及分类；`--target-id`可查询指定已有账单。

| 动作 | 用法与边界 |
| --- | --- |
| 重新处理 | `--action retry`；可能重新分类；UNKNOWN只记录核实意图 |
| 忽略 | `--action ignore`；邮件动作作用整封邮件，检查其全部诊断状态 |
| 接纳来源 | `--action accept-source`；仅针对当前来源项 |
| 关联已有交易 | `--action link --target-id ID`；仍校验账户、金额与时间 |
| 确认另一笔新交易 | `--action confirm-new`；明确允许通过重复候选检查后新建 |
| 修正账户 | 交易`--action retry --account-id ID`；仍校验币种、状态及账户可用性 |

每个resolve必须携带恰好一个有效快照项与非空`--reason`；状态变化报冲突，必须重新查询并决定，不自动重放。可用动作来自同一应用规则，不可处理的对账项保持只读。后台解析提交前也核对邮件状态，不能覆盖已提交的人工忽略。

```sh
# 无参数表示执行时全库当前符合条件的重复问题。
./run recheck
# 精确集合复查：不会把查询后新出现的问题加入此次范围。
./run issues --entity-type bank_transactions --code duplicate_candidates --snapshot-out selected.json
./run recheck --snapshot selected.json
# Docker读取宿主机快照，不需要终端。
docker compose exec -T importer ebki recheck --snapshot - < selected.json
```

限定复查去重并逐对象报告scheduled/already_pending/skipped及原因；合法快照中状态改变的项会跳过，非法文件整体拒绝。不承诺整个批次原子提交。复查复用冻结分类，不增加决定版本；通过后可能入账，仍重复或查询失败则暂停。普通同步不会反复复查。

维护命令使用独立 Runtime/Store，不与后台共享数据库连接，退出后不留线程修改数据。快照包含业务信息，不能放入运行日志或公开仓库。

## 持久化、备份与恢复

AI 分类将交易内部标识、商户文本及候选分类 ID/完整路径发送给配置的模型服务；不发送完整邮件、卡号或金额字段，商户文本本身仍可能包含私人信息。rules_only 完全关闭模型调用。应用未提供静态数据加密，分享日志和邮件样本前须脱敏；不要公开 .env、运行配置、原件或凭据。

| 数据 | 位置与作用 |
| --- | --- |
| 来源位置、明细、决定、任务、写入尝试和当前核对结果 | importer PostgreSQL 数据库，恢复状态的权威依据 |
| 原始邮件 | `./data/email`，含私人账务证据 |
| 核对报告 | `./data/reports`，属于可重新生成的派生结果 |
| 应用运行日志 | `./data/logs`，同时输出终端 |
| 历史验收资料 | `./data/archive/acceptance`，仅归档，不参与日常运行 |
| 配对备份 | `./data/backups`，数据库、邮件原件及配置的停机快照，不参与日常运行 |
| 容器终端日志 | Docker `local` 驱动，单文件 10 MB，最多 5 个 |

不保存完整业务审计或已解决问题历史：人工处理只保留最近一次理由，外部尝试保留确切请求及版本。问题随对象恢复或过期结果删除而退出汇总。

目录固定为相对工作目录的 data/email、data/reports、data/logs。应用日志固定按 10 MiB 轮转，保留 5 个归档；Docker 标准输出日志由 Compose 的 logging 配置独立限额。日志轮转不会删除入账状态或原始邮件。维护命令与常驻服务按实现的日志策略输出，不能用“有日志”判断业务提交成功。容器重建后须复用原数据库及挂载目录。

备份前停止 importer，确保没有并发维护写入，再用 PostgreSQL 备份工具保存 **importer 数据库与原始证据目录的同一停机快照**，同时备份配置和所需报告。按自己的秘密管理机制备份环境变量；应用日志单独归档。没有内置 `backup` 或 `restore --verify-first` 命令。

恢复本模型同结构版本的较早备份前先停止 worker，恢复匹配的数据库和证据目录，执行 `restore-audit` 并检查 `issues`，按来源标记核实远端既有交易后再决定恢复写入。较早备份可能缺少备份之后的已成功关联，不能未经核实直接全量重放。`unknown=0` 仅表示已存在任务没有未决结果，不证明备份后新增来源全部核实；恢复自动运行前应按历史范围完成核对；不要用启动 worker 代替只读恢复审计。恢复检查不能保证发现已删除、被人工移除来源标记或时间被改到原发生日相邻查询窗口之外的远端记录；这些情况需要人工核实。

## 开发验证

```sh
uv sync --frozen
uv run python -c 'import subprocess; subprocess.run(["pytest", "tests/unit", "-q"], timeout=60, check=True)'
uv run ruff check src tests scripts
uv run mypy src scripts
uv build
```

后端单元测试设整个命令 60 秒硬超时；外部边界测试使用脱敏合成数据，不将邮箱原件纳入镜像或提交。生产验收仍需验证真实邮箱来源、实际模型结果、目标部署版本、真实写入与回读，以及容器重建和备份恢复。仅容器启动或只读连接成功不算端到端验收。

可选集成测试通过 `EBKI_TEST_DATABASE_URL` 指向隔离 PostgreSQL；测试自动创建并清理随机 schema。`EBKI_LIVE_CONTEXT` 指向仅测试用的 JSON 文件（`base_url`、`token`、`accounts`、`categories`），启用真实账本接口测试；这些测试会创建测试交易，不可连接生产账本。两者未配置时明确跳过对应集成测试。
