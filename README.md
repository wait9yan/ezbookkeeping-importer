# ezBookkeeping Importer

**把招商银行信用卡邮件变成 ezBookkeeping 里的账目。**

通过 IMAP 收取银行邮件，自动提取消费与退款、匹配账户、选择分类，并写入你已有的 ezBookkeeping。每月再用银行账单核对已导入的记录，让日常记账少一些手工录入。

这是一个可自行部署的单用户后台服务，附带中文交互式终端。查账、统计和日常修改分类仍在 ezBookkeeping 中完成；导入器没有独立网页，也不需要开放服务端口。

[功能与支持范围](#功能与支持范围) · [快速开始](#快速开始) · [Docker 部署](#docker-部署) · [日常使用](#日常使用) · [常见问题](#常见问题) · [详细运维文档](docs/operations.md)

## 功能与支持范围

- **自动收取邮件**：首次扫描邮箱历史邮件，之后增量同步；只下载银行候选邮件的全文，并保存原始邮件以便核查。
- **按卡号和币种匹配账户**：人民币、美元消费分别进入对应账户，退款记为负支出。配置还款账户后，可将成功还款记为转账。
- **规则优先，AI 辅助分类**：你指定的商户规则优先，未命中时由模型从已有分类中选择；也可以完全使用规则。
- **查重与异常处理**：发现疑似重复、账户歧义或不可信来源时暂停相关记录，通过终端菜单核实后继续。
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

> **首次运行前请注意：** 当前版本为 `0.1.0`。启动 `./run`、`worker` 或 Docker 服务后，会自动处理历史邮件和已有待写任务，并实际写入账本，没有 dry-run 模式。请先核对历史交易、信用卡初始负债和其他导入渠道，避免重复记账。

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
mkdir -p data
cp config.example.toml data/config.toml
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

然后编辑 [data/config.toml 的示例](config.example.toml)。不使用 AI 时，将文件顶层的配置改为：

```toml
classification_mode = "rules_only"
```

纯规则模式下，未命中商户规则的消费会进入「待分类」，后续可在 ezBookkeeping 调整。自定义商户规则、还款账户映射和生效日期的示例均在配置模板中。还款导入还需要确认渠道归属并设置 `repayment_ownership_confirmed = true`；保持默认 `false` 时，还款会等待处理，不影响正常消费导入。

保持 `[mail]` 中的 `source_id` 稳定，它用于识别同一逻辑邮箱的采集位置。

### 4. 初始化并检查

```sh
./run migrate
./run doctor
```

`migrate` 初始化 importer 数据库。若数据库不存在，会尝试创建连接串中指定的数据库，此时账号需要 `CREATEDB` 和同实例 `postgres` 维护库的连接权限；也可由管理员预建空库。建表及同版本结构校验需要相应 schema 权限和目标数据库的 `CREATE` 权限。

当前只支持空库初始化和同版本重复初始化，不会自动升级旧数据库或清除已有数据。详细权限与迁移说明见[运维文档](docs/operations.md#配置与本地开发)。

`doctor` 校验启动配置并只读连接数据库和 ezBookkeeping。它**不连接 IMAP、不调用模型，也不测试实际入账**。

### 5. 启动并确认结果

确认账户、分类和历史重复边界后启动：

```sh
./run
```

这会同时启动后台 worker 和中文交互控制台。首次扫描所有可选 IMAP 文件夹；后续按 UID 增量采集。上海时间 17:00 至午夜每 10 分钟检查，其余时段每小时检查，重启会立即恢复处理。

在控制台输入 `status` 查看进度，输入 `issues` 处理异常，并到 ezBookkeeping 核对实际生成的账目。采集完成不等于全部入账成功。输入 `exit` 会一起退出控制台和 worker；当前处理阶段结束前可能需要等待数分钟。

## Docker 部署

**当前尚未发布首个版本。** 本地源码构建和 Linux AMD64/ARM64 镜像验证已通过；GHCR 镜像、GitHub Release 和匿名拉取仍待首发验收。当前使用本节末尾的源码构建命令；下面的预构建镜像部署步骤适用于正式发布后。

正式发布后的镜像地址为 `ghcr.io/wait9yan/ezbookkeeping-importer`。使用发布镜像的部署机器只需 Docker Compose，无需安装 Python、uv 或克隆源码。

正式发布后，先阅读 [Releases](https://github.com/wait9yan/ezbookkeeping-importer/releases) 中的版本说明，再从对应版本标签的仓库复制 `compose.yaml`、`config.example.toml` 和 `.env.example` 到独立部署目录，并参考该版本的 `docs/operations.md`。首次填写配置：

```sh
cp .env.example .env
mkdir -p data
cp config.example.toml data/config.toml
```

按上面的步骤填写服务连接、业务配置，准备账户和分类。已有配置不要重复覆盖。生产 [compose.yaml](compose.yaml) 默认拉取预构建镜像 `latest`，该标签仅在正式版本发布时更新；需要固定版本或回退时，在 `.env` 设置 `EBKI_IMAGE` 为完整版本或 digest 引用。

Compose **只启动 importer**，连接现有外部网络 `bookkeeping`。请准备该网络，并确保 PostgreSQL 和 ezBookkeeping 可从容器访问；实际网络名不同时，修改 `networks.bookkeeping.name`。服务地址应填写容器能访问的主机名或 IP，容器内的 `127.0.0.1` 指向 importer 自己。

容器使用 `10001:10001` 身份，统一将 `./data` 挂载到 `/app/data`，按需创建 `email`、`reports`、`logs` 子目录。已有默认路径数据无需搬迁；旧部署若只给子目录写权限，还需赋予 `data` 根目录写权限。Compose 仅挂载整个 `data` 目录，配置位于 `data/config.toml`，容器内为 `/app/data/config.toml`。首次启动前按上面的步骤复制示例配置；程序不会自动生成配置，也不会回退读取根目录的旧文件。配置随数据目录可写，部署前准备写权限：

```sh
mkdir -p data
sudo chown -R 10001:10001 data

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

`migrate` 只支持空库初始化与同结构校验，不是自动升级旧数据库的工具；`doctor` 不检查 worker 活性或 IMAP/AI 连通性。需要暂停导入时执行 `docker compose stop importer`。Compose 为停止预留 5 分钟，但处理大批次可能更久；这不是停机时间保证。

升级前执行 `docker compose pull importer` 拉取最新正式镜像，停止旧 worker，配对备份数据库、邮件和配置，确认版本兼容后再校验并执行 `docker compose up -d --no-build` 应用更新；仅拉取镜像不会更新正在运行的容器。复用原数据库和 `data/`，不要并行运行两个 worker。回退镜像不会撤销已写入 ezBookkeeping 的交易，操作步骤见[升级与回退](docs/operations.md#镜像升级与回退)。

旧部署升级前先停止 importer，把原根目录的 `config.toml` 移到 `data/config.toml`，再重建容器；目标文件已存在时先人工核对，不要覆盖。本地 `./run` 与 Docker 现在使用相同默认路径，仍可通过 `--config` 显式指定其他文件。`.env` 和可分享的 `config.example.toml` 继续保留在根目录。

从源码开发时，显式使用构建覆盖文件：

```sh
docker compose -f compose.yaml -f compose.build.yaml build
docker compose -f compose.yaml -f compose.build.yaml run --rm importer migrate
docker compose -f compose.yaml -f compose.build.yaml up -d --no-build
```

源码构建同样需要准备配置、外部网络和持久化目录。镜像发布维护说明见[镜像构建与发布](docs/operations.md#镜像构建与发布)。

## 日常使用

在 `./run` 的交互控制台中输入：

| 命令 | 作用 |
| --- | --- |
| `status` | 查看采集、解析、交易和任务进度 |
| `issues` | 打开问题菜单，用方向键、Enter 和 Esc 选择对象与处理方式 |
| `sync` | 请求一次同步，由 worker 执行 |
| `recheck` | 对当前疑似重复及其查询失败记录安排一次复查，通过检查后可继续入账 |
| `help` | 查看命令说明 |
| `exit` | 退出控制台并停止本次启动的 worker |

问题菜单支持查看重复候选、关联已有交易、确认新建、修正账户，以及接纳、忽略或重试邮件。可用操作取决于当前状态；写入结果不明的记录需要先核实，不能直接重新创建。

脚本或另一终端可使用单次命令，输出为 JSON：

```sh
./run status
./run issues
./run sync --since 2026-06-01 --until 2026-06-30
./run recheck
```

日期补扫按**邮件接收日期**筛选，包含起止两天，两个日期必须同时提供；它不限定消费日期，也不改变常规同步的全历史范围。`sync` 和 `recheck` 返回只代表请求已安排，后续处理需要 worker 运行。

无人值守可使用 `./run worker`。`./run worker --once` 只运行一个周期，**同样会实际写入账本**。同一数据库只允许一个 worker；已有 Docker worker 时，不要再用本地 `./run` 启动第二个。如需 Docker 中的人工交互，先停止后台服务，再运行 `docker compose run --rm importer run`，退出后用 `docker compose up -d` 恢复后台运行。

## 数据与隐私

| 数据 | 存放位置 |
| --- | --- |
| 采集进度、交易事实、处理决定、写入状态、当前核对结果 | importer PostgreSQL 数据库 |
| 银行候选邮件原件 | `data/email/` |
| 核对报告 | `data/reports/` |
| 运行日志 | `data/logs/`，同时输出到终端或 Docker 日志 |

AI 分类会把交易内部标识、商户文本和候选分类的 ID、完整分类路径发送给你配置的模型服务。当前分类请求不包含完整邮件、卡号或金额字段；商户文本本身仍可能包含私人信息。选择 `rules_only` 可完全关闭模型调用。

邮件和数据库包含私人账务信息，应用没有提供静态数据加密。`.env`、实际 `data/config.toml` 和运行数据已被 Git 忽略，分享问题时仍需对日志和邮件样本脱敏。

备份应在停止 worker、确保没有并发维护写入后，配对保存 **importer 数据库、邮件原件与配置**。恢复较早备份时，先执行 `./run restore-audit` 并核实远端已有交易，再恢复自动运行。该命令不会创建远端交易，但不能代替完整的历史核对。具体流程和限制见[备份与恢复](docs/operations.md#持久化备份与恢复)。

## 常见问题

**为什么 `./run issues` 没有菜单？**

单次 CLI 只输出 JSON。人工处理入口是在 `./run` 启动的交互控制台里输入 `issues`。

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

旧版配置的移除项、数据库结构和详细运行行为见[配置与运维参考](docs/operations.md)。

## 开发与验证

项目使用 Python、Pydantic、PostgreSQL、httpx、Beautiful Soup、Rich 和 prompt-toolkit。代码按职责分层：

```text
src/ezbookkeeping_importer/
├── domain/        # 交易、金额、账户与邮件身份
├── application/   # 采集、解析、分类、写入、核对与人工处理
├── adapters/      # IMAP、招行模板、模型、ezBookkeeping 和 PostgreSQL
└── entrypoints/   # CLI、交互控制台与后台 worker
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
