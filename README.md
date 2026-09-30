# ezBookkeeping Importer

**把招商银行信用卡邮件变成 ezBookkeeping 里的账目。**

通过 IMAP 收取银行邮件，自动提取消费与退款、匹配账户、选择分类，并写入你已有的 ezBookkeeping。每月再用银行账单核对已导入的记录，让日常记账少一些手工录入。

这是一个可自行部署的单用户后台服务，附带单次维护命令和中文文本展示。查账、统计和日常修改分类仍在 ezBookkeeping 中完成；导入器没有独立网页，也不需要开放服务端口。

[功能与支持范围](#功能与支持范围) · [快速开始](#快速开始) · [Docker 部署](#docker-部署) · [日常使用](#日常使用) · [常见问题](#常见问题) · [详细运维文档](docs/operations.md)

## 功能与支持范围

- **自动收取邮件**：首次扫描邮箱历史邮件，之后增量同步；只下载银行候选邮件的全文，并保存原始邮件以便核查。
- **按卡号和币种匹配账户**：人民币、美元消费分别进入对应账户，退款记为负支出。配置还款账户后，可将成功还款记为转账。
- **规则优先，AI 辅助分类**：你指定的商户规则优先，未命中时由模型从已有分类中选择；也可以完全使用规则。
- **查重与异常处理**：发现疑似重复、账户歧义或不可信来源时暂停相关记录，通过维护命令核实后继续。
- **月账单核对**：对照银行结算记录检查导入结果；符合条件的美元消费可按银行实际人民币结算金额更新。
- **中断后恢复**：保存采集和处理进度。写入结果不明时先查询账本核实，避免直接重发创建请求。

| 项目 | 当前支持 |
| --- | --- |
| 银行 | 招商银行信用卡 |
| 邮件 | `每日信用管家`、`自动还款扣款通知`、`招商银行信用卡电子账单`的已适配 HTML 模板 |
| 邮箱 | IMAP 采集；目前自动来源认证适配 `imap.qq.com` |
| 币种 | 消费与退款支持 CNY、USD；还款支持 CNY |
| 分类 | Python 正则商户规则，以及提供 `/chat/completions` 接口的 OpenAI 兼容模型服务 |
| 运行方式 | 本地 Python + uv，或 Docker Compose |

目前不支持其他银行模板、CSV/PDF 导入或手工 `.eml` 导入。其他 IMAP 主机可以连接采集，但尚未适配的来源认证会形成待处理问题。月账单用于核对，不会仅凭月账单自动补建缺少日报证据的消费。

> **首次运行前请注意：** 当前包版本为 `0.2.1`；以下数据库自动迁移说明对应尚未发布的源码改动。启动 `./run` 或 Docker 服务后，会自动处理历史邮件和已有待写任务，并实际写入账本，没有 dry-run 模式。请先核对历史交易、信用卡初始负债和其他导入渠道，避免重复记账。

## 如何工作

```mermaid
flowchart LR
    A[IMAP 银行邮件] --> B[来源认证与解析]
    B --> C[账户匹配与分类]
    C --> D[查重与校验]
    D --> E[写入 ezBookkeeping]
    B --> F[月账单核对]
    E --> F
    D --> G[待处理问题]
    G --> H[终端人工核实]
```

邮件提供交易事实，规则或模型负责选择消费分类。模型不决定交易金额，也不直接操作账本。遇到无法确定的结果，程序会保留问题供你处理。

## 快速开始

### 1. 准备依赖

以下本地步骤面向 macOS / Linux，需要 **Python 3.12+** 和 **uv**，以及：

- 一个已运行的 ezBookkeeping 实例及其 API Token。
- 一个供导入器单独使用的 PostgreSQL 数据库；可以共用数据库实例，但不要共用 ezBookkeeping 的业务数据库。
- 开启 IMAP 的 QQ 邮箱、邮箱地址及 IMAP 授权码，并确保银行邮件保留在可读取的文件夹中。
- 若使用默认 AI 分类模式，准备模型 API 地址、模型名称和 API Key；纯规则模式不需要模型服务。

获取源码后，在项目根目录执行以下命令。已有配置时请保留现有文件，不要重复覆盖。

```sh
cp .env.example .env
uv sync --frozen
```

### 2. 填写服务连接

编辑 `.env`。全部变量及注释见 [.env.example](.env.example)。

| 变量 | 填写内容 |
| --- | --- |
| `EBKI_DATABASE_URL` | importer 数据库连接串，例如 `postgresql://importer:YOUR_PASSWORD@127.0.0.1:5432/ezbookkeeping_importer` |
| `EBKI_LEDGER_URL` | ezBookkeeping 站点根地址，可包含部署子路径，不要追加 `/api/v1` |
| `EBKI_LEDGER_TOKEN` | ezBookkeeping API Token |
| `EBKI_IMAP_USERNAME` | QQ 邮箱地址 |
| `EBKI_IMAP_PASSWORD` | QQ 邮箱 IMAP 授权码 |
| `EBKI_AI_URL` | 模型 API 基址，通常以 `/v1` 结尾；程序会追加 `/chat/completions` |
| `EBKI_AI_MODEL` | 模型服务提供的模型名称 |
| `EBKI_AI_TOKEN` | 模型 API Key |

IMAP 主机和端口默认是 `imap.qq.com:993`。`.env` 保存连接信息，`data/config.toml` 保存业务规则，两者不要混写。`./run` 会通过 uv 加载项目 `.env`；普通 `uv run` 不会自动加载它。

### 3. 准备账户与分类

在 ezBookkeeping 中完成以下设置：

1. **填写银行卡号**：在可记账账户的描述中写入对应完整卡号（12–19 位，允许空格或连字符），并设置正确币种。导入器根据邮件的完整卡号或末四位匹配，必须唯一命中。
2. **准备外币账户**：有美元消费时，为同一张卡准备 USD 账户；CNY 交易也需要对应 CNY 账户。程序不会在初次入账时自动换算币种。
3. **创建待分类分类**：确保存在唯一且父子均可用的二级支出分类 `其他杂项 → 待分类`。即使使用纯规则模式也需要它；程序不会自动创建分类。

业务配置会在首次执行正式命令时自动创建为 `data/config.toml`，直接使用[程序内置默认值](src/ezbookkeeping_importer/config.toml)。本地源码、已安装的 `ebki` 和 Docker 均使用同一初始化逻辑；已有文件不覆盖。不使用 AI 时，在文件生成后、启动服务前将顶层配置改为：

```toml
classification_mode = "rules_only"
```

纯规则模式下，未命中商户规则的消费会进入「待分类」，后续可在 ezBookkeeping 调整。自定义商户规则、还款账户映射和生效日期的说明均在生成的配置注释中。还款导入还需要确认渠道归属并设置 `repayment_ownership_confirmed = true`；保持默认 `false` 时，还款会等待处理，不影响正常消费导入。

保持 `[mail]` 中的 `source_id` 稳定，它用于识别同一逻辑邮箱的采集位置。

### 4. 数据库准备与检查

```sh
./run migrate
./run doctor
```

当前源码的 `run` 与 `migrate` 共用版本化数据库准备流程：明确指定的目标库缺失时尝试创建，空库初始化，已有受支持旧版本按顺序升级。直接启动 `./run` 也会自动准备数据库；上面的单独迁移与检查适合先验证环境、再开始实际导入。

缺库需要 `CREATEDB` 与同实例 `postgres` 维护库的连接权限；初始化需要 schema 建表权限，升级现有对象需要相应所有者/迁移权限。数据库已经是程序要求的版本时，结构检查只读，不额外要求建库或建 schema 权限。已有 v1 库首次升级迁移历史也需要修改结构；普通账号权限不足时，由管理员先运行 `migrate`，再使用普通账号启动。

`doctor` 只读检查启动配置、数据库迁移历史和表结构就绪，以及账本读取连通性；未初始化、待升级或不兼容结构会明确失败。它不执行迁移、不连接 IMAP、不调用模型，也不测试实际入账。自动迁移不会推测修复损坏结构或自动降级。详细权限与升级说明见[运维文档](docs/operations.md#数据库初始化与版本迁移)。

### 5. 启动并确认结果

确认账户、分类和历史重复边界后启动：

```sh
./run
```

这会在当前进程持续运行导入任务，不启动交互控制台。首次扫描所有可选 IMAP 文件夹；后续按 UID 增量采集。上海时间 17:00 至午夜每 10 分钟检查，其余时段每小时检查，重启会立即恢复处理。

在另一终端执行 `./run status` 查看进度、`./run issues` 查看问题，并到 ezBookkeeping 核对实际账目。采集完成不等于全部入账成功。本地按 Ctrl+C 请求停止；未确定的账本结果在下次启动时核实，不直接重发。

## Docker 部署

**本节部署流程适用于 [v0.2.0](https://github.com/wait9yan/ezbookkeeping-importer/releases/tag/v0.2.0) 及后续兼容版本。** GHCR 镜像支持 Linux AMD64/ARM64，发布前两种架构均需通过镜像验收。Release 提供版本说明，无部署附件；使用仓库中对应版本的部署示例。下面可直接使用预构建镜像部署，也可使用本节末尾的源码构建命令。

发布镜像地址为 `ghcr.io/wait9yan/ezbookkeeping-importer`。使用发布镜像的部署机器只需 Docker Compose，无需安装 Python、uv 或克隆源码。

从 `0.2.0` 起，所有部署方式都会自动生成默认业务配置；`0.1.0` 镜像仍需按该版本文档复制 `config.example.toml` 为 `data/config.toml`。

已发布的 `0.2.1` 新增 Docker 数据目录权限自动准备，采用启动时 root 初始化、随后普通用户运行的模式。发布说明见 [v0.2.1](docs/releases/v0.2.1.md)，本次数据库自动迁移尚未发布，可使用本节末尾的源码构建流程验证；已发布的 `0.2.0` 仍需在首次部署时执行 `sudo chown -R 10001:10001 data`。

先阅读 [Releases](https://github.com/wait9yan/ezbookkeeping-importer/releases) 中的版本说明，再从对应版本标签的仓库复制 `compose.yaml` 和 `.env.example` 到独立部署目录，并参考该版本的 `docs/operations.md`。首次准备服务连接：

```sh
cp .env.example .env
mkdir -p data
```

填写 `.env` 中的服务连接，准备账户和分类。镜像首次执行命令时会自动生成业务配置并继续运行，默认使用 AI 分类、上海时区、12:00 日期默认时间和 `qq-primary` 邮箱来源。个性化还款映射、商户规则或 `rules_only` 模式可在生成后编辑 `data/config.toml`；已有文件不会被覆盖。生产 [compose.yaml](compose.yaml) 默认拉取预构建镜像 `latest`，该标签仅在正式版本发布时更新；需要固定版本或回退时，在 `.env` 设置 `EBKI_IMAGE` 为完整版本或 digest 引用。

Compose **只启动 importer**，连接现有外部网络 `ezbookkeeping`。请准备该网络，并确保 PostgreSQL 和 ezBookkeeping 可从容器访问；实际网络名不同时，修改 `networks.ezbookkeeping.name`。服务地址应填写容器能访问的主机名或 IP，容器内的 `127.0.0.1` 指向 importer 自己。

Compose 统一将 `./data` 挂载到 `/app/data`。新启动入口会先以 root 准备应用所需的目录和文件权限，再通过 `gosu` 切换为 `10001:10001` 执行命令；常见的 `root:root、755` 空目录无需手动改权限。权限修复限于 importer 的配置和 `email`、`reports`、`logs`，会按需修改宿主机文件的所有权和权限，不改文件内容。配置仍由普通 CLI 自动生成，已有配置始终保留。

当前源码中，默认 `run` 还会自动创建缺失的 importer 数据库并执行已提供的迁移；已发布 `0.2.1` 仍需先手动执行 `migrate`。

需要自行管理权限时，可在 Compose 显式设置非 root `user: "UID:GID"`，镜像会保留该身份并跳过 root 权限准备。只读挂载或存储权限禁止修复时明确失败，不自动切换为 root 运行服务。首次初始化和检查：

```sh
mkdir -p data

docker compose pull importer
docker compose run --rm importer migrate
docker compose run --rm importer doctor
```

确认可以自动写入后启动服务：

```sh
docker compose up -d --no-build
docker compose run --rm importer status
docker compose logs --tail 100 importer
```

容器默认运行单进程 `ebki run`，不依赖终端和标准输入；Compose 继承镜像默认命令，不重复设置 `command`。初始化后应用保持 PID 1，日常 `exec … ebki` 同样经过降权入口。维护使用独立命令，执行完退出，不影响后台导入：

```sh
docker compose exec -T importer ebki status
docker compose exec -T importer ebki issues
docker compose logs -f importer
```

持续暂停执行 `docker compose stop importer`，恢复执行 `docker compose up -d`。保留 `restart: unless-stopped`，Docker 重启会恢复此前未被手动停止的服务。Compose 不设置 `init`、`stdin_open`、`tty` 或 `stop_grace_period`，停止采用 Docker 默认十秒期限。账本、AI 和 IMAP 请求超时固定三十秒，与停止期限独立；十秒内不保证请求完成，超过期限可能强制终止，下次启动先核实未确定的账本写入，无法确认的保留为 `UNKNOWN`，不会盲目重发。`docker logs` 和 `data/logs/worker.jsonl` 提供结构化运行事件。

当前源码的 `migrate` 支持初始化及有明确脚本的顺序升级；升级需旧 worker 停止。`doctor` 不检查 worker 活性或 IMAP/AI 连通性。

升级前执行 `docker compose pull importer` 拉取最新正式镜像，停止旧 worker，配对备份数据库、邮件和配置，确认版本兼容后再校验并执行 `docker compose up -d --no-build` 应用更新；仅拉取镜像不会更新正在运行的容器。复用原数据库和 `data/`，不要并行运行两个 worker。回退镜像不会撤销已写入 ezBookkeeping 的交易，操作步骤见[升级与回退](docs/operations.md#镜像升级与回退)。

所有部署方式默认使用 `data/config.toml`，首次正式命令自动创建缺失的默认文件。可通过 `--config` 显式指定其他已准备好的文件，指定文件缺失时明确失败。`--help` 不创建配置。服务连接仍通过根目录 `.env` 或进程环境提供，不写入生成的 TOML。

当前从源码构建时，准备上述配置、外部网络和数据目录后，显式使用构建覆盖文件：

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose -f compose.yaml -f compose.build.yaml run --rm importer migrate
docker compose -f compose.yaml -f compose.build.yaml run --rm importer doctor
docker compose -f compose.yaml -f compose.build.yaml up -d --no-build
```

源码构建后，启动、恢复、重建或新建维护容器均继续使用 `docker compose -f compose.yaml -f compose.build.yaml` 前缀，确保选择本地镜像；不带构建覆盖的 `pull/up/run` 命令适用于发布镜像。查询已运行容器仍可使用 `docker compose exec -T importer ebki ...`。镜像发布维护说明见[镜像构建与发布](docs/operations.md#镜像构建与发布)。

## 日常使用

在 shell 中执行单次命令，默认输出 JSON，需要中文表格时显式加 `--format text`：

```sh
./run status --format text
./run issues --format text
./run sync --since 2026-06-01 --until 2026-06-30
./run recheck
```

无参数 `recheck` 安排全库当前符合条件的重复候选复查；返回只代表请求已安排，后续处理依赖后台服务。日期补扫按**邮件接收日期**筛选，包含起止两天，两个日期必须同时提供，不限定消费日期，也不改变常规自动扫描范围。

人工处理使用查看时的快照，避免后台状态变化后误操作。以下 `交易ID`、`已有账单ID` 应替换成查询到的真实值：

```sh
./run issues show --entity-type bank_transactions --entity-id 交易ID --code duplicate_candidates > selected.json
./run issues candidates --snapshot selected.json --format text
./run issues resolve --snapshot selected.json --action link --target-id 已有账单ID --reason '已核对为同一笔交易'
```

单对象处理要求快照恰好一项；遇到多条诊断时，保留选中项的完整 `issue/state/view` 内容，不能用列表序号作为持久身份。关联已有账单、确认新建、忽略、接纳来源和重新处理分别使用 `link/confirm-new/ignore/accept-source/retry`；修正账户使用交易 `retry --account-id 账户ID`。每个处理动作必须填写非空理由，实际可用动作由当前状态决定。过期快照会报冲突，必须重新查看，不自动重放。

仅复查选定集合时先导出快照，再明确提交该集合：

```sh
./run issues --entity-type bank_transactions --code duplicate_candidates --snapshot-out selected.json
./run recheck --snapshot selected.json
```

Docker 内采用相同参数。宿主机快照可以从标准输入传入：

```sh
docker compose exec -T importer ebki recheck --snapshot - < selected.json
```

快照含业务信息，请妥善保存。命令中断可能已有部分结果提交，应重新查询，不能假定全部回滚。运行入口只有 `run`，没有 `console`、`worker`、`worker --once`、`exit` 命令；不要使用 `attach` 操作服务。完整命令和停止恢复说明见[运维文档](docs/operations.md#日志与单次维护命令)。

## 数据与隐私

| 数据 | 存放位置 |
| --- | --- |
| 采集进度、交易事实、处理决定、写入状态、当前核对结果 | importer PostgreSQL 数据库 |
| 银行候选邮件原件 | `data/email/` |
| 核对报告 | `data/reports/` |
| 运行日志 | `data/logs/`，同时输出到终端或 Docker 日志 |

AI 分类会把交易内部标识、商户文本和候选分类的 ID、完整分类路径发送给你配置的模型服务。当前分类请求不包含完整邮件、卡号或金额字段；商户文本本身仍可能包含私人信息。选择 `rules_only` 可完全关闭模型调用。

邮件和数据库包含私人账务信息，应用没有提供静态数据加密。`.env`、实际 `data/config.toml` 和运行数据已被 Git 忽略，分享问题时仍需对日志和邮件样本脱敏。

备份应在停止 run 后台服务、确保没有并发维护写入后，配对保存 **importer 数据库、邮件原件与配置**。恢复较早备份时，先执行 `./run restore-audit` 并核实远端已有交易，再恢复自动运行。该命令不会创建远端交易，但不能代替完整的历史核对。具体流程和限制见[备份与恢复](docs/operations.md#持久化备份与恢复)。

## 常见问题

**为什么 `./run issues` 没有菜单？**

项目已改为单次命令：`issues` 列表、`issues show` 查看快照、`issues candidates` 对比、`issues resolve` 处理。需要中文展示可加 `--format text`。

**能只导入某个日期之后的交易吗？**

当前常规同步首次扫描全历史，没有全局消费日期下限。`sync --since/--until` 是邮件日期补扫，不能用它限制自动导入范围。

**模型失败会自动归到「待分类」吗？**

不会。只有正常返回「无法匹配」，或纯规则模式下没有命中规则时，才使用「待分类」。接口失败、无效响应和未知分类 ID 都会形成异常。

**可以导入转发的银行邮件吗？**

已知主题带 `Fw:`、`Fwd:` 或 `转发：` 前缀时会尝试解析，但仍需可信来源或人工接纳。转发者通过邮箱认证不等于原银行邮件可信。

**美元消费之后如何核对？**

首次按原币写入同卡 USD 账户。月账单出现唯一匹配的可信人民币结算记录时，可保留原交易 ID，更新为同卡 CNY 账户和银行实际金额。结算会保留当前分类、备注、标签和图片。匹配歧义、缺少账户或目标交易的账户、金额、时间等关键字段已被改动时，会保留问题等待处理；详见[账务行为](docs/operations.md#账务行为)。

**删除了 ezBookkeeping 里的交易，会自动补回来吗？**

不会自动恢复被删除的远端交易。写入结果不明时也不会直接重发；程序会先按来源标记查询并核实。

完整配置契约、数据库结构和详细运行行为见[配置与运维参考](docs/operations.md)。

## 开发与验证

项目使用 Python、Pydantic、PostgreSQL、httpx、Beautiful Soup、Rich。代码按职责分层：

```text
src/ezbookkeeping_importer/
├── domain/        # 交易、金额、账户与邮件身份
├── application/   # 采集、解析、分类、写入、核对与人工处理
├── adapters/      # IMAP、招行模板、模型、ezBookkeeping 和 PostgreSQL
└── entrypoints/   # 单次维护CLI与后台运行入口
migrations/        # 数据库初始化 SQL
tests/             # 单元测试与集成测试
```

```sh
uv sync --frozen
uv run python -c 'import subprocess; subprocess.run(["pytest", "tests/unit", "-q"], timeout=60, check=True)'
uv run ruff check src tests scripts
uv run mypy src scripts
uv build
```

后端单元测试使用 60 秒硬超时。集成测试可通过 `EBKI_TEST_DATABASE_URL` 指向隔离 PostgreSQL，并运行 `uv run pytest tests/integration -q`；未配置时相关测试会跳过。`EBKI_LIVE_CONTEXT` 用于真实账本接口测试，会创建测试交易，只能指向测试账本，详见[开发验证](docs/operations.md#开发验证)。

提交问题时请附上运行方式、脱敏错误信息和复现步骤。涉及邮件模板时使用脱敏样本，不要提交邮箱原件、银行卡号、授权码或 API Token。
