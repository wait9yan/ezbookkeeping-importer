# ezBookkeeping 招行邮件导入器

单用户后台服务：通过 IMAP 读取邮箱中的招商银行信用卡邮件，将日报消费与退款导入已有 ezBookkeeping 账户，成功还款邮件记为转账，月账单用于核对。日常查账和修改分类在 ezBookkeeping 完成；此应用没有独立网页或监听端口。

这是首期实现。上线前应完成生产邮箱来源、模型分类、小批量真实写入与回读、重启和备份恢复验收。服务连接成功或合成测试通过不能代替账务端到端验证。先核对下述初始化参数，再启用真实写入。

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
- `config.toml` 保存业务规则：还款映射、商户规则、分类模式和稳定邮件来源身份。`mail.source_id` 是稳定业务身份，仍保留在 TOML。

```sh
cp .env.example .env
cp config.example.toml config.toml
uv sync --frozen
```

先填写 `.env` 的数据库连接。`migrate`、`status`、`issues` 和 `sync` 不要求账本、邮箱或模型凭据；启动前再补齐对应服务。项目启动器固定使用项目目录的 `.env` 和默认 `config.toml`，日常只需：

```sh
./run migrate   # 首次初始化
./run doctor    # 检查配置与只读连通性
./run           # 同时启动 worker 和交互控制台
```

`./run` 通过 uv 加载项目 `.env`，有参数时透传给 `ebki`；例如 `./run status`、`./run issues`。默认配置路径无需重复指定；需要其他配置时使用 `./run --config /path/to/config.toml run`。启动不会自动初始化数据库。

**普通 `uv run` 不会替本项目自动加载 `.env`。** 原始命令仍可使用 `uv run --env-file .env ebki run`；应用只读取 TOML 和进程环境，不另设 dotenv 加载器，已有 shell 环境变量优先。直接执行已安装的 `ebki` 时，调用方负责注入环境。`.env` 已被忽略，不会进入源码发行包或 Docker 镜像；`.env.example` 是可分享的空值模板。

### 服务连接环境变量

| 环境变量 | 用途与默认值 |
| --- | --- |
| `EBKI_DATABASE_URL` | importer PostgreSQL 连接串；所有实际维护命令必需 |
| `EBKI_LEDGER_URL` | ezBookkeeping 站点根地址，可含部署子路径，不加 `/api/v1` |
| `EBKI_LEDGER_TOKEN` | ezBookkeeping API Token |
| `EBKI_LEDGER_TIMEOUT_SECONDS` | 账本 HTTP 超时，默认 30 秒 |
| `EBKI_AI_URL`、`EBKI_AI_MODEL`、`EBKI_AI_TOKEN` | 模型地址、模型名、API Key；地址通常以 `/v1` 结尾 |
| `EBKI_AI_TIMEOUT_SECONDS` | 模型 HTTP 超时，默认 30 秒 |
| `EBKI_IMAP_HOST`、`EBKI_IMAP_PORT` | 默认 `imap.qq.com`、`993` |
| `EBKI_IMAP_USERNAME`、`EBKI_IMAP_PASSWORD` | IMAP 登录名及凭据；QQ 使用邮箱地址和 IMAP 授权码 |
| `EBKI_IMAP_TIMEOUT_SECONDS` | IMAP 超时，默认 30 秒 |

日志级别在 `config.toml` 顶层设置 `log_level = "INFO"`，支持 `DEBUG/INFO/WARNING/ERROR/CRITICAL`，修改后重启进程。目录固定为相对工作目录的 `data/email`、`data/reports`、`data/logs`；应用日志固定按 10 MiB 轮转，保留 5 个归档。

端口、超时和日志级别在使用前校验。必需项缺失时列出变量名，不回显输入秘密。按命令检查依赖：

| 命令 | 必需服务配置 |
| --- | --- |
| `migrate/status/issues/sync/recheck` | 数据库；不需要配置外部服务，recheck只安排复查 |
| `resolve` 的本地接纳、忽略、普通重试、确认新建 | 数据库；决定实际执行仍交后台 |
| `resolve --action link`、`resolve --account-id ...`、`restore-audit` | 数据库与账本 |
| `run`、`worker`、`doctor` | 数据库、账本、邮箱；`classification_mode="ai"` 时额外要求模型地址、名称及 Key |

`EBKI_AI_URL` 应填写模型服务的 API 基址，应用追加 `/chat/completions`。如果站点根地址返回 HTML 首页，即使 HTTP 200 也不能通过分类校验；应核对服务实际 API 路径（常见为 `/v1`），不要把网页地址当作 API。

`doctor` 检查完整启动配置以及数据库、账本读取连通性；不把配置存在当成 IMAP 或模型调用验证。`classification_mode="rules_only"` 时不要求模型服务配置，也不会调用模型。

### 业务初始化

核对 ezBookkeeping 账户描述及 `config.toml` 中的业务设置：

1. 在 ezBookkeeping 可记账子账户的描述中填写银行卡号（12–19 位，可含空格或连字符分组），并设置正确币种。按卡号和币种唯一匹配，不再填写 `[[accounts]]`。同尾号同币种多个候选、或缺少同币账户会明确报错；历史换卡记录仍需核实，不按今天的账户名称猜归属。
2. 核对历史消费与初始负债、既有交易和其他导入渠道的重复边界。退款自动按负支出处理，仍执行查重；还款需要配置账户映射和渠道归属。
3. 还款两端人民币账户、二级转账分类，以及仅日期通知的 `date_only_time` 记账约定。
4. 当前支持 CNY 与 USD，美元需要对应 USD 子账户；同一张卡只有 USD 子账户时，其 CNY 消费不能自动写入 USD 账户。新入账不再需要报价时效配置。
5. 保持 `mail.source_id` 稳定。IMAP 来源认证按连接主机自动选择：当前 `imap.qq.com` 使用 QQ 收件链及 SPF/DKIM/DMARC 结果检查，通过后自动接纳；不自行执行 DKIM 公钥验签。来源缺失、失败或无法判断时进入异常处理。

启动 worker 后自动采集、解析、分类、匹配账户、查重并写入通过校验的交易，包括已有待写任务。缺少配置、匹配歧义或结果不明的记录保留为异常。

日常自动采集使用 IMAP，主机、端口和凭据由环境变量指定，无人工接纳运行模式或来源策略开关。目前内置 QQ 来源认证；其他主机仍能连接采集，但尚未适配的认证会明确产生来源异常，不能将 QQ 邮件头直接当成其他邮箱的可信依据。来源认证和人工接纳绑定具体 IMAP 来源项。同一业务来源、同一原件只要存在可信或已接纳来源项即可继续处理，不改写其他位置的认证结论。

首次常规同步全量扫描所有可选邮箱文件夹，之后按 UID 增量扫描。首次范围上界固定，`status` 的历史完成展示由该范围内来源项派生；增量不扩大首次范围，UIDVALIDITY 改变后建立新的首次范围。采集完成不等于解析或入账完成。`[mail] rescan_days = 7` 指定跨日回扫窗口，设为 `0` 禁用回扫；UIDVALIDITY 改变时重新全量扫描。`sync --since/--until` 仅用于手工补扫，不改变常规扫描游标。

### 从旧版配置迁移

删除 `source_policy` 和 `trusted_authserv_id`；认证由 IMAP 主机自动选择。旧字段明确提示迁移，已有持久来源异常仍需逐项处理，不自动追认旧邮件。

删除 `writes_enabled`、`refund_ownership_confirmed` 和仅服务于旧写入开关的 `historical_boundary_reviewed`。这些旧键会明确报迁移错误，不会忽略原来关闭写入的配置后直接启动自动写入。完成配置迁移并启动 worker 即采用自动处理行为。

删除普通消费的 `[[accounts]]` 配置，在 ezBookkeeping 对应账户的描述中填写银行卡号；已有决定保留冻结账户和币种，不因描述修改自动重新映射。旧账户映射会明确报告迁移提示。`repayments` 仍保留。新交易直接原币入账，旧人民币暂估的已保存任务仍可恢复和结算。

将 TOML 中 `ledger_url/ai_url/ai_model`、`mail.host/port/username/timeout_seconds` 等服务连接字段移到上述环境变量，并从 TOML 删除旧字段；已有四个秘密变量名保持不变。旧键不会被静默覆盖或忽略，即使同时设置了环境变量，也会报告需要迁移的键与目标变量。程序不会自动改写现有 `config.toml` 或 `.env`。

目录和日志轮转参数不再对外配置，请删除 TOML 中的 `evidence_dir/report_dir/log_dir/log_max_bytes/log_backups`。旧 `.env` 中的 `EBKI_EVIDENCE_DIR/EBKI_REPORT_DIR/EBKI_LOG_DIR/EBKI_LOG_MAX_BYTES/EBKI_LOG_BACKUPS/EBKI_LOG_LEVEL` 已不读取；日志级别改到 TOML 顶层。Docker 网络和用户直接通过 Docker 配置管理，删除旧 `EBKI_DOCKER_NETWORK/EBKI_UID/EBKI_GID`。使用过自定义目录的部署，切换前应停止 worker，将已有数据迁入固定目录或调整 Compose 的宿主机挂载源，容器目标路径保持 `/app/data/*`。

## 启动和维护

Dockerfile 固定 Python `3.12.13`、uv `0.11.21`，使用仓库 `uv.lock` 执行 `uv sync --frozen --no-dev`。首次启动前显式迁移；worker 不代替迁移步骤。Compose 自动读取项目 `.env`，并通过 `environment` 注入与本地相同的变量；必需凭据由应用按命令检查，因此可以在尚未填写邮箱和模型凭据时运行迁移。

部署时把服务地址改为已有 Docker 网络内可访问的名称，例如 `http://ezbookkeeping:8080`，不能沿用容器内的 `127.0.0.1`。Compose 使用已有外部网络 `bookkeeping`；实际名称不同时直接修改 `compose.yaml` 中的 `networks.bookkeeping.name`。

容器默认沿用 Dockerfile 的 `10001:10001` 身份。首次部署准备挂载目录及权限：

```sh
mkdir -p data/email data/reports data/logs
sudo chown -R 10001:10001 data/email data/reports data/logs
```

如需以宿主机当前用户运行，用 `id -u` 和 `id -g` 查看 ID，在 `compose.yaml` 的 `services.importer` 下显式设置 `user: "实际UID:实际GID"`，并确保挂载目录允许该身份写入。

Compose 将宿主机 `./data/*` 挂载到容器 `/app/data/*`，应用在 `/app` 工作目录下使用固定相对路径。特殊部署可调整挂载源，容器目标保持固定。Compose 不另起 PostgreSQL 服务或创建外部网络；目标库由 `migrate` 按上述权限初始化。

```sh
docker compose build
docker compose run --rm importer migrate
docker compose run --rm importer doctor
docker compose up -d
docker compose run --rm importer status
docker compose run --rm importer issues
```

worker 正常运行即自动写入；需要暂停时停止 worker（`docker compose stop importer`）。`doctor`、`status`、`issues` 和 `restore-audit` 不执行交易创建。停止或重建容器不会自动回滚远端交易。

```sh
docker compose run --rm importer sync
docker compose run --rm importer sync --since 2026-06-01 --until 2026-06-30
docker compose logs --tail 100 importer
docker compose restart importer
```

`sync` 提交同步请求，不能把命令返回视为全部入账成功。 `--since` 与 `--until` 必须同时提供，格式为 `YYYY-MM-DD`，包含起止两天；筛选依据是 IMAP 邮件接收日期（INTERNALDATE 的日期部分），不是消费发生日期。区间补扫使用独立持久任务，与普通同步串行执行，不改变历史扫描上界、游标或完成状态；重复请求沿用相同来源去重。省略日期时继续原有全历史／增量流程，不附加日期下限。`status`、`issues` 读取数据库中的进度和异常；`doctor` 负责连接诊断，运行成功也不等于邮件到真实写入的完整验收。异常处理入口为：

```sh
docker compose run --rm importer resolve --help
docker compose run --rm importer restore-audit --help
```

`resolve` 支持 `accept-source`（接纳来源）、`confirm-new`（核实候选后确认新建）、`link`（关联已有账单）、`ignore`（明确忽略）和 `retry`（纠正明确失败后重试）。操作引用明确对象类型、对象 ID 和当前版本，并保留最新处理原因；`dispatching`、`unknown` 不能借此绕过结果核实直接重发。待创建记录可在 `retry` 时用 `--account-id` 修正账户；已入账交易不提供此修改。

`issues` 只汇总业务对象的当前问题，输出 `entity_type`、`entity_id`、问题代码和状态/版本；不提供独立数字问题 ID 或已解决问题历史。`resolve` 以对象类型和 ID 定位，具体参数以 `resolve --help` 为准。来源接纳针对 `email_source_item`，交易处理针对 `bank_transactions`，避免把不同实体的相同数字误认为同一对象。

```sh
docker compose run --rm importer issues --entity-type email_source_item --entity-id 12
docker compose run --rm importer resolve email_source_item 12 --action accept-source --reason "已核对原邮件来源"
docker compose run --rm importer resolve bank_transactions AbCdEfGh1234_-XY --version 1 --action link --target-id 123456 --reason "已核对同一笔账本记录"
```

示例对象 ID 和版本仅作说明；实际使用 issues 输出中的对象类型和 ID；交易与账本任务还须传当前 `--version`，来源、邮件等无版本对象不需要此参数。不能使用旧全局问题 ID。`issues` 不再提供 `--all` 已解决历史查询。

正式邮件入口只有 IMAP，不提供 `import-eml` 命令；原始证据仍保存为 `.eml`。`Fw:`、`Fwd:`、`转发：` 前缀的已知银行主题也会保存原件并尝试解析，原始主题保留；转发邮件须有可信的来源项或人工接纳，不能因转发者通过认证就自动入账。

数据库连接断开后，worker 会立即以失败退出，避免继续持有失效运行时。Compose 的 `restart: unless-stopped` 会重启进程并重新获取排他锁，未完成写入先进入 UNKNOWN 核实；本地直接运行时需重新执行 worker 命令。

## 日志与交互控制台

worker记录采集、解析、分类、写入、核对及恢复事件。正常进度最多每5秒一次，阶段开始/结束和错误立即输出；空闲轮询保持安静，逐条正常细节通过在 `config.toml` 顶层设置 `log_level = "DEBUG"` 并重启查看。JSONL文件保留结构化事件；交互终端用中文标题显示，非交互worker标准输出保持JSON，适合Docker日志收集。

本地执行 `./run`，在一个终端中启动 worker、显示日志并接受命令。控制台管理本次启动的 worker，后台进程异常会立即报告；已有 worker 占用同一数据库时启动失败，不会接管已有进程。

```sh
./run
```

支持 `help`、`status`、`issues`、`sync`、`recheck`、`resolve`、`exit`；业务参数与单次CLI相同，支持Tab补全和本次会话的命令历史。日志从本次启动前的位置继续显示，包含本次 worker 启动事件。

交互控制台使用中文摘要和表格显示结果。`status` 展示采集、解析、交易及任务进度；`issues` 默认按原因汇总，区分诊断条数与受影响对象数，并给出下一步操作。需要查看交易详情时使用 `issues --entity-type bank_transactions`，也可沿用 `--entity-id` 定位对象。`help` 显示中文命令说明与最少参数示例。单次CLI（例如 `./run status`）继续输出JSON，便于脚本调用。

删除或修正ezBookkeeping中的重复候选后，在控制台执行一次：

```text
recheck
```

该命令批量安排当前重复候选及其复查查询失败对象的一轮检查，无需逐笔填写ID、版本或原因。保留已有分类和冻结决定，不重新调用AI；worker重新完整查询候选，通过正常校验后才入账。仍然重复或查询失败就继续保持问题状态，启动、普通轮询和 `sync` 不会自动反复检查。已入账、已忽略及写入结果不明的交易不会被重新创建。

输入 `exit`、按 Ctrl+C 或关闭输入（EOF）都会一起退出控制台和 worker。程序先停止接收新命令，等待已接受的维护命令和当前处理阶段结束，不再进入后续阶段；等待时仍显示日志。当前阶段可能是批量采集、分类或写入，退出可能需要数分钟，不默认强杀处理进程。

命令执行期间日志仍可显示，重复提交业务命令会明确提示当前忙。独立 `console` 命令和 `quit` 已移除。无人值守运行使用 `./run worker` 或 `docker compose up -d`；只执行一个完整周期可用 `./run worker --once`，该方式也包含实际账本写入。查询和维护使用单次CLI。

`sync` 反馈“已排队”或“已有请求已合并”，不表示采集完成。`recheck` 反馈本次安排、已在处理和跳过数量，不表示已经完成查重或入账。`resolve` 反馈具体处理决定；结果不明时仍明确等待核实，不把保存决定显示为账本写入成功。日志中的“采集完成”“写入尝试已登记”“写入已核实”“既有关联已恢复”分别对应不同阶段。核对结果发布与报告文件导出也分别记录。

日志文件仅由worker写入和轮转，控制台只读尾随；状态与问题仍以数据库为准。修改级别需更新 `config.toml` 顶层 `log_level` 并重启 worker，控制台不能恢复此前未被记录的DEBUG事件。

## 持久化、备份与恢复

| 数据 | 位置与作用 |
| --- | --- |
| 来源位置、明细、决定、任务、写入尝试和当前核对结果 | importer PostgreSQL 数据库，恢复状态的权威依据 |
| 原始邮件 | `./data/email` 挂载，含私人账务证据 |
| 核对报告 | `./data/reports` 挂载，属于可重新生成的派生结果 |
| 应用运行日志 | `./data/logs` 挂载，同时输出终端 |
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
uv run ruff check src tests
uv run mypy src
uv build
```

后端单元测试设整个命令 60 秒硬超时；外部边界测试使用脱敏合成数据，不将邮箱原件纳入镜像或提交。生产验收仍需验证真实邮箱来源、实际模型结果、目标部署版本、真实写入与回读，以及容器重建和备份恢复。仅容器启动或只读连接成功不算端到端验收。

可选集成测试通过 `EBKI_TEST_DATABASE_URL` 指向隔离 PostgreSQL；测试自动创建并清理随机 schema。`EBKI_LIVE_CONTEXT` 指向仅测试用的 JSON 文件（`base_url`、`token`、`accounts`、`categories`），启用真实账本接口测试；这些测试会创建测试交易，不可连接生产账本。两者未配置时明确跳过对应集成测试。
