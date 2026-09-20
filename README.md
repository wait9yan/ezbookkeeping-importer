# ezBookkeeping 招行邮件导入器

单用户后台服务：读取 QQ 邮箱中的招商银行信用卡邮件，将日报消费与退款导入已有 ezBookkeeping 账户，成功还款邮件记为转账，月账单用于核对。日常查账和修改分类在 ezBookkeeping 完成；此应用没有独立网页或监听端口。

这是首期实现，尚未完成生产 QQ 邮箱、用户模型服务及实际部署镜像的联合验收。已提供的本地邮件样本和独立测试账本用于开发验证，不能代表生产配置已接通。先核对下述初始化参数，再启用真实写入。

## 账务行为

- 首次扫描全部可读取 IMAP 文件夹，按 UID 保存下载进度；历史消费纳入补记。重启立即恢复，上海时间 17:00 至午夜每 10 分钟检查，其余时段每小时检查。
- 分类采用用户规则与模型；若明确选择 `classification_mode="rules_only"` 则只运行规则。缺少必需模型配置会形成异常；有效的无法匹配结果使用已有的「其他杂项 → 待分类」二级支出分类。接口失败、无效 JSON、未知分类 ID 均为异常，不冒充无法匹配。该分类路径必须唯一且父子分类均可用，应用不自动创建分类。
- 日报退款为负支出；还款为付款账户到信用卡的转账。退款、还款跨渠道归属和历史生效账户需要单独配置。尚未确定规则的外币退款保留为异常。
- 外币消费按 ezBookkeeping 参考汇率用 Decimal 换算为人民币，保存报价快照；月账单唯一匹配后覆盖原交易金额，保留回读得到的分类、时间、备注、标签、图片等字段。不检测人工金额修改冲突。
- 写入超时或进程中断进入 `unknown`，通过来源标记查询并回读核实，不直接重发 POST。月账单缺日报证据不会自动补建消费；远端删除的交易不会被自动复活。

## 配置与本地开发

需要独立的 PostgreSQL 数据库，以及用户已有的 ezBookkeeping 服务。可以复用 PostgreSQL 实例，但不要使用 ezBookkeeping 业务数据库保存 importer 状态。

`migrate` 会初始化连接串指定的 importer 数据库：目标库存在时直接迁移；明确不存在时，使用同一账号连接同实例的 `postgres` 维护库，创建 `EBKI_DATABASE_URL` 中显式指定的数据库，再回连建表。首次建库需要该账号具备 `CREATEDB` 权限及维护库连接权限；已有库只需连接和目标 schema 的建表权限。权限不足会明确报错，可由管理员预建目标库后重试。应用不创建角色，其他命令不隐式建库；认证或网络失败也不会触发创建。

配置分为两个明确来源：

- `.env` 保存服务连接和运行环境参数，由启动工具注入进程环境。
- `config.toml` 保存业务规则：账户/还款映射、商户规则、分类模式、来源策略、历史边界确认及是否启用写入。`mail.source_id` 是稳定业务身份，仍保留在 TOML。

```sh
cp .env.example .env
cp config.example.toml config.toml
uv sync --frozen
```

先填写 `.env` 的数据库连接。`migrate`、`status`、`issues`、`sync` 和 `import-eml` 不要求账本、邮箱或模型凭据；启动 worker 前再补齐对应服务。使用以下命令显式加载本机 `.env`：

```sh
uv run --env-file .env ebki --config config.toml migrate
uv run --env-file .env ebki --config config.toml doctor
uv run --env-file .env ebki --config config.toml worker
```

**普通 `uv run` 不会替本项目自动加载 `.env`。** 应用不再另设 dotenv 加载器，只读取进程环境；已有 shell 环境变量优先于文件中的同名值。直接执行已安装的 `ebki` 时，调用方负责注入环境。`.env` 已被忽略，不会进入源码发行包或 Docker 镜像；`.env.example` 是可分享的空值模板。

### 服务与运行环境变量

| 环境变量 | 用途与默认值 |
| --- | --- |
| `EBKI_DATABASE_URL` | importer PostgreSQL 连接串；所有实际维护命令必需 |
| `EBKI_LEDGER_URL` | ezBookkeeping 站点根地址，可含部署子路径，不加 `/api/v1` |
| `EBKI_LEDGER_TOKEN` | ezBookkeeping API Token |
| `EBKI_LEDGER_TIMEOUT_SECONDS` | 账本 HTTP 超时，默认 30 秒 |
| `EBKI_AI_URL`、`EBKI_AI_MODEL`、`EBKI_AI_TOKEN` | 模型地址、模型名、API Key；地址通常以 `/v1` 结尾 |
| `EBKI_AI_TIMEOUT_SECONDS` | 模型 HTTP 超时，默认 30 秒 |
| `EBKI_IMAP_HOST`、`EBKI_IMAP_PORT` | 默认 `imap.qq.com`、`993` |
| `EBKI_IMAP_USERNAME`、`EBKI_IMAP_PASSWORD` | QQ 邮箱地址及 IMAP 授权码，不是登录密码 |
| `EBKI_IMAP_TIMEOUT_SECONDS` | IMAP 超时，默认 30 秒 |
| `EBKI_EVIDENCE_DIR`、`EBKI_REPORT_DIR`、`EBKI_LOG_DIR` | 本地默认 `var/evidence`、`var/reports`、`var/logs`；相对当前工作目录 |
| `EBKI_LOG_MAX_BYTES`、`EBKI_LOG_BACKUPS` | 默认 10 MiB、5 个归档 |
| `EBKI_DOCKER_NETWORK`、`EBKI_UID`、`EBKI_GID` | 仅供 Compose 使用的网络与容器用户，本地应用不读取 |

端口、超时和轮转数值在使用前校验。必需项缺失时列出变量名，不回显输入秘密。按命令检查依赖：

| 命令 | 必需服务配置 |
| --- | --- |
| `migrate/status/issues/sync/import-eml` | 数据库；不需要配置外部服务 |
| `resolve` 的本地接纳、忽略、普通重试、确认新建 | 数据库；决定实际执行仍交后台 |
| `resolve --action link`、`resolve --account-id ...`、`restore-audit` | 数据库与账本 |
| `worker`、`doctor` | 数据库、账本、邮箱；`classification_mode="ai"` 时额外要求模型地址、名称及 Key |

`doctor` 检查完整启动配置以及数据库、账本读取连通性；不把配置存在当成 IMAP 或模型调用验证。`classification_mode="rules_only"` 时不要求模型服务配置，也不会调用模型。

### 业务初始化

编辑 `config.toml`，核对以下业务事实：

1. 卡片标识、原币、目标人民币账户 ID 及历史生效日期，不把今天的映射应用到换卡前。
2. 历史消费与初始负债、既有交易和其他导入渠道的重复边界，完成后设置 `historical_boundary_reviewed=true`；退款、还款分别确认渠道归属。
3. 还款两端人民币账户、二级转账分类，以及仅日期通知的 `date_only_time` 记账约定。
4. `exchange_rate_max_age_hours` 报价时效；没有有效报价时明确异常，不按 1:1 换算。
5. 来源策略与稳定的 `mail.source_id`。默认 `manual_acceptance`；启用 `qq_authentication_results` 前确认可信 QQ 接收链及 `trusted_authserv_id`，该策略不执行独立 DKIM 验签。

初次保持 `writes_enabled=false`，配置和业务范围核对完成后再按下文启用。

### 从旧版配置迁移

将 TOML 中 `ledger_url/ai_url/ai_model`、`mail.host/port/username/timeout_seconds`、目录及日志轮转字段移到上述环境变量，并从 TOML 删除旧字段；已有四个秘密变量名保持不变。旧键不会被静默覆盖或忽略，即使同时设置了环境变量，也会报告需要迁移的键与目标变量。程序不会自动改写现有 `config.toml` 或 `.env`。

## 启动和维护

Dockerfile 固定 Python `3.12.13`、uv `0.11.21`，使用仓库 `uv.lock` 执行 `uv sync --frozen --no-dev`。首次启动前显式迁移；worker 不代替迁移步骤。Compose 自动读取项目 `.env`，并通过 `environment` 注入与本地相同的变量；必需凭据由应用按命令检查，因此可以在尚未填写邮箱和模型凭据时运行迁移。

部署时把服务地址改为已有 Docker 网络内可访问的名称，例如 `http://ezbookkeeping:8080`，不能沿用容器内的 `127.0.0.1`。设置 `EBKI_DOCKER_NETWORK`，准备挂载目录，并使用对应属主：

```sh
mkdir -p var/evidence var/reports var/logs
export EBKI_UID="$(id -u)"
export EBKI_GID="$(id -g)"
```

未设置 `EBKI_*_DIR` 时，Compose 将宿主机 `./var/*` 挂载到容器 `/app/var/*`。若覆盖目录，值必须是已准备好权限的绝对路径，Compose 将同一绝对路径挂载到容器并传给应用。Compose 不另起 PostgreSQL 服务或创建外部网络；目标库由 `migrate` 按上述权限初始化。

```sh
docker compose build
docker compose run --rm importer migrate
docker compose run --rm importer doctor
docker compose up -d importer
docker compose run --rm importer status
docker compose run --rm importer issues
```

初次保持 `writes_enabled=false`，检查连接、解析结果、分类及账户映射。确认真实写入条件后改为 `true` 并重启 importer；关闭该项可暂停新的账本写入，既有待核实结果仍需要核实。停止或重建容器不会自动回滚远端交易。

```sh
docker compose run --rm importer sync
docker compose run --rm importer sync --since 2026-06-01 --until 2026-06-30
docker compose logs --tail 100 importer
docker compose restart importer
```

`sync` 提交同步请求，不能把命令返回视为全部入账成功。 `--since` 与 `--until` 必须同时提供，格式为 `YYYY-MM-DD`，包含起止两天；筛选依据是 IMAP 邮件接收日期（INTERNALDATE 的日期部分），不是消费发生日期。区间补扫使用独立持久任务，与普通同步串行执行，不改变历史扫描上界、游标或完成状态；重复请求沿用相同来源去重。省略日期时继续原有全历史／增量流程，不附加日期下限。`status`、`issues` 读取数据库中的进度和异常；`doctor` 负责连接诊断，运行成功也不等于邮件到真实写入的完整验收。异常处理入口为：

```sh
docker compose run --rm importer resolve --help
docker compose run --rm importer import-eml --help
docker compose run --rm importer restore-audit --help
```

`resolve` 支持 `accept-source`（接纳来源）、`confirm-new`（核实候选后确认新建）、`link`（关联已有账单）、`ignore`（明确忽略）和 `retry`（纠正明确失败后重试）。操作引用当前异常和决定版本并保留原因；`dispatching`、`unknown` 不能借此绕过结果核实直接重发。待创建记录可在 `retry` 时用 `--account-id` 修正账户；已入账交易不提供此修改。

```sh
docker compose run --rm importer issues --id 12
docker compose run --rm importer resolve 12 --version 1 --action accept-source --reason "已核对原邮件来源"
docker compose run --rm importer resolve 13 --version 1 --action link --target-id 123456 --reason "已核对账本中同一笔"
```

示例 ID 仅示意，使用实际异常、版本与目标 ID。

本地原件通过 `import-eml` 保存并进入同一处理流程；Docker 中需将原件目录额外只读挂载到容器后传容器内路径。本地原件不继承 IMAP 的来源验证结论，需在异常中明确接纳。`Fw:`、`Fwd:`、`转发：` 前缀的已知银行主题也会保存原件并尝试解析，原始主题保留；转发邮件必须明确接纳，不能因转发者通过邮箱认证而自动入账。

## 持久化、备份与恢复

| 数据 | 位置与作用 |
| --- | --- |
| 来源位置、明细、决定、任务、写入尝试、审计 | importer PostgreSQL 数据库，恢复状态的权威依据 |
| 原始邮件 | `./var/evidence` 挂载，含私人账务证据 |
| 核对报告 | `./var/reports` 挂载 |
| 应用运行日志 | `./var/logs` 挂载，同时输出终端 |
| 容器终端日志 | Docker `local` 驱动，单文件 10 MB，最多 5 个 |

应用日志的轮转由 `EBKI_LOG_MAX_BYTES` 与 `EBKI_LOG_BACKUPS` 控制；日志轮转不会删除入账状态或原始邮件。维护命令与常驻服务按实现的日志策略输出，不能用“有日志”判断业务提交成功。容器重建后须复用原数据库及挂载目录。

备份前停止 importer，确保没有并发维护写入，再用 PostgreSQL 备份工具保存 **importer 数据库与原始证据目录的同一停机快照**，同时备份配置和所需报告。按自己的秘密管理机制备份环境变量；应用日志单独归档。没有内置 `backup` 或 `restore --verify-first` 命令。

恢复旧备份后，先保持 `writes_enabled=false`，恢复匹配的数据库和证据目录，执行 `restore-audit` 并检查 `issues`，按来源标记核实远端既有交易后再决定恢复写入。旧数据库可能缺少备份之后的已成功关联，不能未经核实直接全量重放。`unknown=0` 仅表示已存在任务没有未决结果，不证明备份后新增来源全部核实；保持写入关闭，重新采集并按历史范围核对之后再恢复。恢复检查不能保证发现已删除、被人工移除来源标记或时间被改到原发生日相邻查询窗口之外的远端记录；这些情况需要人工核实。

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
