# ezBookkeeping Importer

**为 [ezBookkeeping](https://github.com/mayswind/ezbookkeeping) API 设计的招商银行信用卡账单导入工具。**

通过 IMAP 收取银行邮件，自动提取消费与退款、匹配账户、选择分类并写入已有账本，再用月账单核对导入结果。
这是可自行部署的单用户后台服务，没有独立网页或服务端口；查账、统计和修改分类仍在 ezBookkeeping 中完成。

[功能与支持范围](#功能与支持范围) · [快速开始](#快速开始) · [Docker 部署](#docker-部署) · [日常使用](#日常使用) · [运维参考](docs/operations.md)

## 功能与支持范围

- **邮件同步**：首次扫描历史邮件，之后增量同步；保存银行候选邮件原件以便核查。
- **账户匹配**：按卡号和原币唯一匹配账户，退款记为负支出；配置还款映射后可将成功还款记为转账。
- **消费分类**：商户规则优先，未命中时由 OpenAI 兼容模型从已有分类中选择；也可完全使用规则。
- **查重与恢复**：重复候选、账户歧义或来源异常形成待处理问题；写入结果不明时先核实，避免直接重发。
- **月账单核对**：检查导入结果，符合条件的美元消费可按银行实际人民币结算金额更新。

| 项目 | 当前支持 |
| --- | --- |
| 银行与邮件 | 招商银行信用卡的 `每日信用管家`、`自动还款扣款通知`、`招商银行信用卡电子账单` 已适配 HTML 模板 |
| 邮箱 | IMAP 采集；自动来源认证目前适配 `imap.qq.com` |
| 币种 | 消费与退款支持 CNY、USD；还款支持 CNY |
| 运行方式 | Python 3.12+ 与 uv，或 Docker Compose（Linux AMD64/ARM64） |

其他银行模板、CSV/PDF 和手工 `.eml` 导入尚不支持。其他 IMAP 主机可连接采集，但未适配的来源认证会形成问题。
月账单用于核对，不会仅凭月账单补建缺少日报证据的消费；详细边界见[账务行为](docs/operations.md#账务行为)。

> **启动会实际写账，没有 dry-run。** 首次常规同步扫描全历史，没有全局消费日期下限；已有待写任务也会继续处理。
> 请先核对历史交易、信用卡初始负债和其他导入渠道，避免重复记账。

## 快速开始

### 1. 准备服务与账户

本地步骤面向 macOS / Linux，除 Python 3.12+ 和 uv 外，还需要：

- 已运行的 ezBookkeeping 及 API Token。
- 独立的 importer PostgreSQL 数据库；可以共用实例，不要共用 ezBookkeeping 业务数据库。
- 开启 IMAP 的 QQ 邮箱、邮箱地址和授权码，银行邮件保留在可读取的文件夹中。
- 默认 AI 分类所需的 API 地址、模型名和 Key；纯规则模式不需要模型服务。

在 ezBookkeeping 中准备：

1. 在可记账账户描述中写入完整卡号（12–19 位，可含空格或连字符），并设置正确币种；按完整卡号或末四位必须唯一命中。
2. 有美元消费时，为同卡准备 USD 账户，CNY 消费需要 CNY 账户；首次入账不自动换算币种。
3. 创建唯一且父子均可用的二级支出分类 **其他杂项 → 待分类**，纯规则模式也需要；程序不会自动创建分类。

### 2. 配置并准备数据库

在项目根目录执行；已有文件请保留：

```sh
cp .env.example .env
uv sync --frozen
```

编辑 `.env` 填写服务连接，变量及默认值见 [.env.example](.env.example)。账本地址使用站点根地址，不加 `/api/v1`；模型基址通常以 `/v1` 结尾。
`./run` 通过 uv 加载项目 `.env`；普通 `uv run` 不会自动加载它。

```sh
./run migrate
```

首次正式命令生成 `data/config.toml`，已有文件不覆盖。迁移只要求数据库配置，权限说明见[数据库初始化与版本迁移](docs/operations.md#数据库初始化与版本迁移)。
启动前编辑生成的配置：还款映射、商户规则等说明见文件注释，保持 `mail.source_id` 稳定。
还款还需确认渠道归属并设置 `repayment_ownership_confirmed = true`；默认会暂停还款，正常消费仍可导入。

不使用 AI 时，将顶层配置改为：

```toml
classification_mode = "rules_only"
```

未命中规则的消费进入「待分类」，之后可在 ezBookkeeping 调整；模型接口失败会形成异常。

### 3. 检查与启动

```sh
./run doctor
./run
```

`doctor` 校验完整启动配置，并只读检查数据库就绪与账本读取连通性；不执行迁移，不连接 IMAP、不调用模型、不测试实际入账。
`./run` 在当前进程持续导入；另一终端用 `status`、`issues` 查看进度，并到 ezBookkeeping 核对实际账目。采集完成不等于全部入账成功。
本地按 Ctrl+C 请求停止，未确定的账本结果在下次启动核实。

## Docker 部署

部署机器只需 Docker Compose。从 [GitHub 仓库](https://github.com/wait9yan/ezbookkeeping-importer)复制 `compose.yaml`、`.env.example` 到独立目录。
默认使用 GHCR 镜像，可在 `.env` 通过 `EBKI_IMAGE` 指定其他镜像。

Compose **只启动 importer**，需要已有外部网络 `ezbookkeeping`；网络名不同时修改 `networks.ezbookkeeping.name`。
PostgreSQL 和 ezBookkeeping 必须能从该网络访问；服务地址使用容器可访问的主机名或 IP，容器内 `127.0.0.1` 指向 importer 自己。

```sh
cp .env.example .env
mkdir -p data
```

填写 `.env` 并按上文准备账户和分类，再执行：

```sh
docker compose pull importer
docker compose run --rm importer migrate
```

此时可编辑生成的 `data/config.toml`，设置商户规则、还款映射或 `rules_only`，然后检查并启动：

```sh
docker compose run --rm importer doctor
docker compose up -d --no-build
docker compose exec -T importer ebki status
docker compose logs --tail 100 importer
```

`./data` 挂载到 `/app/data`。镜像自动准备必要目录权限后以普通用户运行；权限细节见[运维参考](docs/operations.md#启动和维护)。
暂停用 `docker compose stop importer`，恢复用 `docker compose up -d`；维护命令退出不影响后台服务。
源码构建须使用额外 Compose 文件，完整命令见[源码部署](docs/operations.md#源码部署)；更新前阅读[镜像升级与回退](docs/operations.md#镜像升级与回退)。

## 日常使用

单次维护默认输出 JSON，添加 `--format text` 可输出中文表格：

```sh
./run status --format text
./run issues --format text
```

Docker 使用相同参数，例如 `docker compose exec -T importer ebki issues --format text`。
日期补扫、重复候选复查和人工处理快照的完整步骤见[日志与单次维护命令](docs/operations.md#日志与单次维护命令)。
维护请求已安排不代表完成入账；人工操作依据查看时的快照，状态变化后必须重新查询。

## 数据与隐私

PostgreSQL 保存进度、交易事实、决定和写入状态；`data/email` 保存邮件原件，`data/reports` 保存核对报告，`data/logs` 保存日志。
AI 分类发送交易内部标识、商户文本及候选分类 ID/完整路径，不发送完整邮件、卡号或金额字段；商户文本仍可能含私人信息。
选择 `rules_only` 可完全关闭模型调用。应用没有静态数据加密；分享日志或邮件样本前请脱敏，不要提交凭据、原件或银行卡号。

停止后台服务并确保没有并发维护写入后，配对备份 **importer 数据库、邮件原件与配置**。
恢复较早备份时，先执行 `./run restore-audit` 并核实远端已有交易，再恢复运行；该命令不能代替完整历史核对，详见[备份与恢复](docs/operations.md#持久化备份与恢复)。

完整配置、维护、开发检查见[配置与运维参考](docs/operations.md)，新增 SQL 迁移见[数据库结构变更与发布](docs/database-migrations.md)。
提交问题时请附运行方式、脱敏错误和复现步骤。
