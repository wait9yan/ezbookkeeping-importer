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

需要独立的 PostgreSQL 数据库，以及用户已有的 ezBookkeeping 服务。可以复用 PostgreSQL 实例，但不要使用 ezBookkeeping 业务数据库保存 importer 状态。

`migrate` 会初始化连接串指定的 importer 数据库：目标库存在时直接迁移；明确不存在时，使用同一账号连接同实例的 `postgres` 维护库，创建 `EBKI_DATABASE_URL` 中显式指定的数据库，再回连建表。首次建库需要该账号具备 `CREATEDB` 权限及维护库连接权限；已有库首次初始化需要连接和目标 schema 建表权限；同版本重复初始化会在同一事务的临时 schema 中从权威 SQL 构造预期结构并比对列、约束、索引及表列注释，因此维护账号还需目标数据库的 CREATE 权限。临时 schema 检查后删除，失败回滚；普通运行命令不执行该校验。权限不足会明确报错，可由管理员预建目标库后重试。应用不创建角色，其他命令不隐式建库；认证或网络失败也不会触发创建。

数据库采用九表结构：`email_sync_checkpoint`、`email_source_item`、`email`、`bank_report`、`bank_transactions`、`background_task`、`ledger_write_attempt`、`bank_statement_reconciliation`、`schema_version`。邮件原件、来源认证、报告事实、导入决定及核对结果分别明确归属；所有表和字段有中文数据库注释。只支持空库初始化与同版本重复初始化，旧或不完整结构明确失败，不自动升级或清库；旧测试环境应使用新的空数据库或空 schema。

配置分为两个明确来源：

- `.env` 保存服务连接和运行环境参数，由启动工具注入进程环境。
- `data/config.toml` 保存业务规则：还款映射、商户规则、分类模式、时区、日志级别和稳定邮件来源身份。`mail.source_id` 是稳定业务身份。

TOML 只接受 [配置示例](../config.example.toml) 中的业务字段。服务连接信息仅从环境变量读取；未知字段统一报 `unknown TOML field`，不会被忽略或改写。

```sh
cp .env.example .env
mkdir -p data
cp config.example.toml data/config.toml
uv sync --frozen
```

先填写 `.env` 的数据库连接。`migrate`、`status`、`issues` 和 `sync` 不要求账本、邮箱或模型凭据；启动前再补齐对应服务。项目启动器固定使用项目目录的 `.env` 和默认 `data/config.toml`，日常只需：

```sh
./run migrate   # 首次初始化
./run doctor    # 检查配置与只读连通性
./run           # 在单进程中持续导入，不读取终端输入
```

`./run` 通过 uv 加载项目 `.env`，有参数时透传给 `ebki`；例如 `./run status`、`./run issues`。默认配置路径无需重复指定；需要其他配置时使用 `./run --config /path/to/config.toml run`。启动不会自动初始化数据库。

**普通 `uv run` 不会替本项目自动加载 `.env`。** 原始命令仍可使用 `uv run --env-file .env ebki run`；应用只读取 TOML 和进程环境，不另设 dotenv 加载器，已有 shell 环境变量优先。直接执行已安装的 `ebki` 时，调用方负责注入环境。`.env` 已被忽略，不会进入源码发行包或 Docker 镜像；`.env.example` 是可分享的空值模板。

本机 `ebki` 安装在项目虚拟环境的 `.venv/bin/ebki`，不会自动成为全局命令。若 shell 提示找不到 `ebki`，日常优先使用 `./run`，或显式通过 uv 调用：

```sh
./run status --format text
uv run --env-file .env ebki status --format text
```

也可执行 `source .venv/bin/activate` 后直接输入 `ebki --help`；激活只将虚拟环境加入当前 shell 的 `PATH`，不会加载 `.env`。需要服务连接配置时，继续使用上述启动器或显式加载环境。

### 服务连接环境变量

| 环境变量 | 用途与默认值 |
| --- | --- |
| `EBKI_DATABASE_URL` | importer PostgreSQL 连接串；所有实际维护命令必需 |
| `EBKI_LEDGER_URL` | ezBookkeeping 站点根地址，可含部署子路径，不加 `/api/v1` |
| `EBKI_LEDGER_TOKEN` | ezBookkeeping API Token |
| `EBKI_AI_URL`、`EBKI_AI_MODEL`、`EBKI_AI_TOKEN` | 模型地址、模型名、API Key；地址通常以 `/v1` 结尾 |
| `EBKI_IMAP_HOST`、`EBKI_IMAP_PORT` | 默认 `imap.qq.com`、`993` |
| `EBKI_IMAP_USERNAME`、`EBKI_IMAP_PASSWORD` | IMAP 登录名及凭据；QQ 使用邮箱地址和 IMAP 授权码 |

日志级别在 `data/config.toml` 顶层设置 `log_level = "INFO"`，支持 `DEBUG/INFO/WARNING/ERROR/CRITICAL`，修改后重启进程。目录固定为相对工作目录的 `data/email`、`data/reports`、`data/logs`；应用日志固定按 10 MiB 轮转，保留 5 个归档。

账本、AI 和 IMAP 客户端超时统一固定为 30 秒，不提供环境变量或 TOML 配置。端口和日志级别在使用前校验。必需项缺失时列出变量名，不回显输入秘密。按命令检查依赖：

| 命令 | 必需服务配置 |
| --- | --- |
| `migrate/status/issues/sync/recheck` | 数据库；不需要配置外部服务，recheck只安排复查 |
| `issues resolve` 的接纳、忽略、普通重试、确认新建 | 数据库；决定实际执行仍交后台 |
| `issues candidates`、`issues resolve` 的关联、账户修正，以及 `restore-audit` | 数据库与账本 |
| `run`、`doctor` | 数据库、账本、邮箱；`classification_mode="ai"` 时额外要求模型地址、名称及 Key |

`EBKI_AI_URL` 应填写模型服务的 API 基址，应用追加 `/chat/completions`。如果站点根地址返回 HTML 首页，即使 HTTP 200 也不能通过分类校验；应核对服务实际 API 路径（常见为 `/v1`），不要把网页地址当作 API。

`doctor` 检查完整启动配置以及数据库、账本读取连通性；不把配置存在当成 IMAP 或模型调用验证。`classification_mode="rules_only"` 时不要求模型服务配置，也不会调用模型。

### 业务初始化

核对 ezBookkeeping 账户描述及 `data/config.toml` 中的业务设置：

1. 在 ezBookkeeping 可记账子账户的描述中填写银行卡号（12–19 位，可含空格或连字符分组），并设置正确币种。按卡号和币种唯一匹配。同尾号同币种多个候选、或缺少同币账户会明确报错；历史换卡记录仍需核实，不按今天的账户名称猜归属。
2. 核对历史消费与初始负债、既有交易和其他导入渠道的重复边界。退款自动按负支出处理，仍执行查重；还款需要配置账户映射和渠道归属。
3. 还款两端人民币账户、二级转账分类，以及仅日期通知的 `date_only_time` 记账约定。
4. 当前支持 CNY 与 USD，美元需要对应 USD 子账户；同一张卡只有 USD 子账户时，其 CNY 消费不能自动写入 USD 账户。首次入账不进行汇率换算。
5. 保持 `mail.source_id` 稳定。IMAP 来源认证按连接主机自动选择：当前 `imap.qq.com` 使用 QQ 收件链及 SPF/DKIM/DMARC 结果检查，通过后自动接纳；不自行执行 DKIM 公钥验签。来源缺失、失败或无法判断时进入异常处理。

启动 run 后台服务后自动采集、解析、分类、匹配账户、查重并写入通过校验的交易，包括已有待写任务。缺少配置、匹配歧义或结果不明的记录保留为异常。

日常自动采集使用 IMAP，主机、端口和凭据由环境变量指定，无人工接纳运行模式或来源策略开关。目前内置 QQ 来源认证；其他主机仍能连接采集，但尚未适配的认证会明确产生来源异常，不能将 QQ 邮件头直接当成其他邮箱的可信依据。来源认证和人工接纳绑定具体 IMAP 来源项。同一业务来源、同一原件只要存在可信或已接纳来源项即可继续处理，不改写其他位置的认证结论。

首次常规同步全量扫描所有可选邮箱文件夹，之后按 UID 增量扫描。首次范围上界固定，`status` 的历史完成展示由该范围内来源项派生；增量不扩大首次范围，UIDVALIDITY 改变后建立新的首次范围。采集完成不等于解析或入账完成。`[mail] rescan_days = 7` 指定跨日回扫窗口，设为 `0` 禁用回扫；UIDVALIDITY 改变时重新全量扫描。`sync --since/--until` 仅用于手工补扫，不改变常规扫描游标。

## 启动和维护

**[v0.1.0](https://github.com/wait9yan/ezbookkeeping-importer/releases/tag/v0.1.0) 已正式发布。** [发布 CI](https://github.com/wait9yan/ezbookkeeping-importer/actions/runs/36671286224) 成功，GHCR 镜像的 Linux AMD64/ARM64 两种架构均已在空 Docker 配置下匿名拉取验证。Release 只提供版本说明，无部署附件；部署示例从对应版本的仓库获取。若选择从源码运行 Docker，先准备本节的配置、外部网络与数据目录，再执行：

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose -f compose.yaml -f compose.build.yaml run --rm importer migrate
docker compose -f compose.yaml -f compose.build.yaml run --rm importer doctor
docker compose -f compose.yaml -f compose.build.yaml up -d --no-build
```

源码部署的启动、恢复、重建及新建维护容器均继续使用 `docker compose -f compose.yaml -f compose.build.yaml` 前缀，确保选择本地镜像；查询已运行容器可直接 `docker compose exec -T importer ebki ...`。下面不带构建覆盖的 GHCR 拉取、up/run 命令用于已发布的预构建镜像。

生产 Compose 默认从 GHCR 拉取 `latest` 预构建镜像，支持 Linux AMD64/ARM64；可在 `.env` 中通过 `EBKI_IMAGE` 指定完整版本或 digest 引用。Dockerfile 使用 Python `3.12.13` 和 uv `0.11.21` 多阶段构建，以锁文件安装生产依赖并校验一致性，运行镜像不携带 uv。首次启动前显式初始化数据库；run 不代替初始化步骤。Compose 自动读取项目 `.env`，并通过 `environment` 注入与本地相同的变量；必需凭据由应用按命令检查，因此可以在尚未填写邮箱和模型凭据时运行迁移。

部署时把服务地址改为已有 Docker 网络内可访问的名称，例如 `http://ezbookkeeping:8080`，不能沿用容器内的 `127.0.0.1`。Compose 使用已有外部网络 `ezbookkeeping`；实际名称不同时直接修改 `compose.yaml` 中的 `networks.ezbookkeeping.name`。

容器默认沿用 Dockerfile 的 `10001:10001` 身份。首次部署准备挂载目录及权限：

```sh
mkdir -p data
sudo chown -R 10001:10001 data
```

如需以宿主机当前用户运行，用 `id -u` 和 `id -g` 查看 ID，在 `compose.yaml` 的 `services.importer` 下显式设置 `user: "实际UID:实际GID"`，并确保挂载目录允许该身份写入。

Compose 统一将宿主机 `./data` 挂载到容器 `/app/data`，应用在 `/app` 工作目录下按需创建 `email`、`reports`、`logs` 子目录。宿主机需赋予 `data` 根目录及子目录写权限，以便创建和写入运行文件。特殊部署可调整挂载源，容器目标保持固定。Compose 不另起 PostgreSQL 服务或创建外部网络；目标库由 `migrate` 按上述权限初始化。Compose 只挂载整个 `data`，配置文件为 `data/config.toml`（容器内 `/app/data/config.toml`），随该目录可写。首次启动前创建 `data` 并将根目录 `config.example.toml` 复制为 `data/config.toml`，填写配置并确保 `10001:10001` 可读取配置、写入数据目录。缺少配置时应用明确失败，不自动生成文件；挂载自动创建空目录不等于配置已就绪。

```sh
docker compose pull importer
docker compose run --rm importer migrate
docker compose run --rm importer doctor
docker compose up -d --no-build
docker compose run --rm importer status
docker compose run --rm importer issues
```

Docker 默认命令为 `run`，Compose 继承镜像默认命令，不重复设置 `command`；直接在PID 1运行单个导入进程，不创建控制台或子worker。维护命令在独立进程执行，关闭输入或命令退出不影响后台：

```sh
docker compose exec -T importer ebki status
docker compose exec -T importer ebki issues --format text
docker compose logs -f importer
```

| 操作 | 结果 |
| --- | --- |
| 单次维护命令结束或中断 | 仅结束当前维护进程；已提交结果保留 |
| `docker compose stop importer` | 请求停止，默认十秒后仍未退出可被强杀，并保持停止 |
| `docker compose up -d` | 启动或恢复服务，核实中断的账本操作 |
| 进程意外退出 | `unless-stopped`自动重启 |
| Docker服务重启 | 恢复此前未被手动停止的服务 |

不使用attach、TTY或常驻stdin；Compose删除init/stdin_open/tty/stop_grace_period。应用自行处理SIGTERM/SIGINT并关闭数据库连接，没有自有子worker需要回收。

worker 正常运行即自动写入；需要持续暂停时执行 `docker compose stop importer`。`doctor`、`status`、`issues` 和 `restore-audit` 不执行交易创建。停止或重建容器不会自动回滚远端交易。

```sh
docker compose run --rm importer sync
docker compose run --rm importer sync --since 2026-06-01 --until 2026-06-30
docker compose logs --tail 100 importer
docker compose restart importer
```

`sync` 提交同步请求，不能把命令返回视为全部入账成功。 `--since` 与 `--until` 必须同时提供，格式为 `YYYY-MM-DD`，包含起止两天；筛选依据是 IMAP 邮件接收日期（INTERNALDATE 的日期部分），不是消费发生日期。区间补扫使用独立持久任务，与普通同步串行执行，不改变历史扫描上界、游标或完成状态；重复请求沿用相同来源去重。省略日期时继续原有全历史／增量流程，不附加日期下限。`status`、`issues` 读取数据库中的进度和异常；`doctor` 负责连接诊断，运行成功也不等于邮件到真实写入的完整验收。异常处理使用下文的单次 `issues show/candidates/resolve` 命令。

问题处理以快照中的身份、版本及完整前置状态为准；发生变化明确报冲突，不自动刷新后提交。`issues`仍为只读列表，人工操作改为显式`issues resolve`，不提供独立问题ID或已解决问题历史。原来的菜单选择、确认与理由输入由命令参数表达，业务校验不因无交互而减少。

正式邮件入口只有 IMAP，不提供 `import-eml` 命令；原始证据仍保存为 `.eml`。`Fw:`、`Fwd:`、`转发：` 前缀的已知银行主题也会保存原件并尝试解析，原始主题保留；转发邮件须有可信的来源项或人工接纳，不能因转发者通过认证就自动入账。

数据库连接断开后，worker 会立即以失败退出，避免继续持有失效运行时。Compose 的 `restart: unless-stopped` 会重启进程并重新获取排他锁，未完成写入先进入 UNKNOWN 核实；本地直接运行时需重新执行 `./run`，重新启动后台进程。

### 配置文件位置

本地 CLI 默认读取工作目录下的 `data/config.toml`；`./run` 会定位项目根目录，Docker 工作目录为 `/app`。使用 `--config FILE` 可以显式选择文件。缺少指定文件时明确报错，程序不生成或修改配置。`.env` 和 `config.example.toml` 位于根目录；备份 `data` 包含业务配置，`.env` 需单独妥善备份。

### 镜像升级与回退

以下流程用于已发布镜像的部署与更新。首次部署从目标版本标签的仓库复制 `compose.yaml`、`config.example.toml` 和 `.env.example` 到独立目录，并参考该版本的 `docs/operations.md`；Compose 默认使用 `latest`。后续升级先阅读 GitHub Release 的版本说明，再对照对应版本的 Compose 和示例配置更新，不直接覆盖现有 `.env` 或 `data/config.toml`。有意修改过外部网络或挂载源时保留部署差异。

1. 阅读最新正式版本的 数据库结构说明，保留 `.env` 的 `EBKI_IMAGE` 为空并执行 `docker compose pull importer` 拉取 `latest`；需要固定目标版本时将该变量设为完整版本或 digest 引用。记录当前运行镜像的版本或 digest，供兼容性允许时回退。拉取失败时先解决问题，仍在运行的旧容器不受影响。
2. 执行 `docker compose stop importer`，确认旧 worker 已停止，且没有并发维护写入，再配对备份 importer 数据库、原始邮件和配置。
3. 对声明兼容的版本执行 `docker compose run --rm importer migrate` 与 `docker compose run --rm importer doctor`。当前 `migrate` 只支持初始化及同结构校验；结构不兼容时会失败，不得通过删库绕过。未来发生 schema 变化的版本必须另行提供迁移方案。
4. 全部检查成功后，执行 `docker compose up -d --no-build` 应用更新；仅拉取不会替换运行中的容器。通过 `status`、`issues` 和日志核查恢复情况。

Compose采用默认十秒停止期限。程序在邮件批次、单笔交易、核实任务和报告边界响应停止；正在进行的网络请求仍保留原30秒超时，十秒内不保证完成。超时强杀后，同步任务重新排队，已提交数据保留；账本写入先转UNKNOWN并在下次启动核实，不自动重发。登记后尚未发送的任务也可能待核实，远端查不到不足以证明未发送。报告JSON可能暂时滞后于数据库，强杀可能遗留临时文件；不承诺所有状态和派生文件自动立即恢复。

回退前先停止新 worker。只有旧镜像仍兼容当前数据库结构时，才把 `EBKI_IMAGE` 改回保存的旧版本/digest，执行拉取、诊断与重建。镜像回退不恢复数据库，也不撤销远端账目；涉及恢复备份时按本文“持久化、备份与恢复”流程先核实远端状态。

### 镜像构建与发布

[v0.1.0](https://github.com/wait9yan/ezbookkeeping-importer/releases/tag/v0.1.0) 对应源码 revision `d84d81061829c0bbc66b840b2135f2c9a6a89ccf`；OCI version 为 `0.1.0`，两种架构的 revision 均一致。已验证多架构索引 digest 为 `sha256:365d256180fe51af261fa8fb3fa402cb007763e02edb7c1de9ac91476c1f4e45`，可用于 `EBKI_IMAGE` 的完整 digest 引用。

GitHub Actions 对 PR、主分支和版本标签运行验证。正式版本标签使用 `vX.Y.Z`，必须与 `pyproject.toml` 的项目版本一致；发布后不覆盖版本，修复使用新版本。维护者先更新版本及锁文件、评审合入，再推送版本标签。

正式发布流程将镜像推送到 `ghcr.io/wait9yan/ezbookkeeping-importer`，版本标签为 `X.Y.Z`，同时记录提交标签和 OCI 源码 revision，并在正式版本发布时更新 `latest`；PR 和主分支验证不会更新该标签。Compose 默认使用 `latest`，需要固定版本或回退时用 `EBKI_IMAGE` 指定版本，严格复现时使用多架构索引 digest。发布工作流使用 `GITHUB_TOKEN`，无需把个人推送凭据或生产连接信息交给 CI。

GHCR package 已设为 Public，v0.1.0 的 AMD64/ARM64 镜像均已验证未登录拉取成功。部署端拉取失败应检查 package 权限和网络，不在生产机器自动回退为源码构建。

CI 对 PR 和主分支执行验证，不发布；版本标签触发的流程在两种架构上验证实际构建镜像，确认 CLI、依赖、时区、非 root 写入、包内 SQL 以及隔离 PostgreSQL 初始化与重复校验，通过后才发布多架构镜像并创建自动生成版本说明的正式 Release，不附部署压缩包。已验证镜像通过内部 artifact 传递给发布 job，不重新构建。v0.1.0 的远端发布流程已成功完成。`schema.sql` 在源码中是链接到 `migrations/001_initial.sql` 的符号链接，构建阶段不能遗漏目标；最终安装环境必须包含可读 SQL。

镜像发布和 GitHub Release 创建分为独立 job。以下是条件失败场景的恢复流程，当前没有实际远端发布失败的验收记录。若镜像已发布而 Release 步骤失败，先检查对应 Release 是否已经创建：

```sh
# 替换为失败运行对应的版本标签。
gh release view v0.1.0 --repo wait9yan/ezbookkeeping-importer
```

确认 Release 尚未创建时，在该次 Actions 运行中选择“Re-run failed jobs”，只补跑 Release，不覆盖正式镜像；鉴权或网络错误不能当作 Release 不存在。若 Release 已创建，核对标签与版本说明，不重复执行创建命令。恢复时不要重推同名 Git 标签、删除正式镜像或把全部发布任务重新运行。

开发机器从源码构建须显式添加 `-f compose.build.yaml`；生产 Compose 不包含 `build`。不要把 `doctor` 当作健康检查，它不证明 worker 活性。独立 `worker` 与 `worker --once` 入口已移除。项目没有独立 HTTP 端口，也不启动第二个 worker 探活。

## 日志与单次维护命令

`./run`或`ebki run`持续运行并输出事件，不创建菜单、提示符或stdin读取循环。非TTY标准输出与`data/logs/worker.jsonl`均为结构化事件；本地TTY日志可用中文展示。日志级别在`data/config.toml`顶层设置，正常进度限频、空闲轮询安静。日志格式和单次命令结果格式独立，状态仍以数据库为准。

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

没有独立console/worker/worker --once或exit入口；后台使用信号或Docker stop管理。维护命令只使用自身Runtime/Store，不与后台共享数据库连接，不在退出后留线程继续修改数据。快照中包含业务信息，不能放入运行日志或公开仓库。

## 持久化、备份与恢复

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

应用日志固定按 10 MiB 轮转，保留 5 个归档；Docker 标准输出日志由 Compose 的 `logging` 配置独立限额。日志轮转不会删除入账状态或原始邮件。维护命令与常驻服务按实现的日志策略输出，不能用“有日志”判断业务提交成功。容器重建后须复用原数据库及挂载目录。

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
