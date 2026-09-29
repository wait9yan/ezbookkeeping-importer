"""维护命令的中文终端视图；只展示应用结果，不查询或修改业务状态。"""

from datetime import date, datetime
from decimal import Decimal

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..application.maintenance import issue_groups

ENTITY_LABELS = {
    "email_source_item": "邮件来源",
    "email": "邮件",
    "bank_transactions": "银行交易",
    "background_task": "后台任务",
    "bank_report": "银行报告",
    "bank_statement_reconciliation": "账单核对项",
}
STATE_LABELS = {
    "pending": "待处理", "skipped": "已跳过", "collected": "已采集",
    "failed": "失败", "parsed": "已解析", "ignored": "已忽略", "issue": "已暂停",
    "queued": "已排队", "dispatching": "发送中", "unknown": "结果待核实",
    "booked": "已入账", "done": "已完成", "rejected": "已拒绝", "cancelled": "已取消",
    "matched": "已匹配", "mismatched": "存在差异", "not_checked": "尚未核验",
    "query_failed": "查询失败", "target_missing": "远端记录不存在",
    "not_comparable": "无法比较", "missing_source_transaction": "缺少日报来源交易",
    "missing_statement_evidence": "缺少月账单证据", "ambiguous": "关联有歧义",
    "awaiting_statement": "等待月账单", "out_of_scope": "不在核对范围",
}
TASK_LABELS = {
    "sync": "邮件同步", "sync_range": "邮件补扫", "create": "创建账单",
    "settle_amount": "结算金额调整", "settle_currency": "结算币种调整",
}
ISSUE_LABELS = {
    "duplicate_candidates": "重复交易候选",
    "duplicate_check_failed": "重复候选复查失败",
    "source_acceptance": "来源待接纳",
    "download_failed": "邮件下载失败",
    "unknown_template": "未知邮件模板",
    "classification_failed": "交易分类失败",
    "account_not_found": "未找到匹配账户",
    "account_ambiguous": "账户匹配有歧义",
    "reconciliation_failed": "月账单核对失败",
    "reconciliation": "账单核对问题",
    "write_unknown": "账本写入待核实",
    "mime_body": "邮件正文结构异常",
    "invalid_template": "邮件模板异常",
    "empty_daily": "日报缺少交易明细",
    "invalid_daily_row": "日报交易行异常",
    "repayment_state": "还款状态待确认",
    "invalid_monthly_row": "月账单行异常",
    "missing_control": "月账单缺少控制总额",
    "invalid_control": "月账单控制总额异常",
    "control_mismatch": "月账单总额不一致",
}
COMMANDS = {
    "status": ("查看处理状态", "只读：采集、解析、交易、任务及问题概况。", "status"),
    "issues": (
        "交互处理当前问题", "进入分组、对象和操作菜单；方向键选择，Esc 返回。",
        "issues\nissues --entity-type bank_transactions\nissues --entity-id ID",
    ),
    "recheck": (
        "批量复查重复候选", "处理远端数据后手动执行一次；复用已有分类，通过后可能入账。"
        "仍重复或查询失败会再次暂停，不会定时复查。", "recheck",
    ),
    "sync": (
        "安排邮件采集", "安排后台同步，可指定接收日期补扫；新邮件通过检查后可能入账。"
        "不会复查已暂停的重复候选。",
        "sync\nsync --since 2026-09-01 --until 2026-09-30",
    ),
    "exit": ("退出项目", "一起关闭 console 和 worker，等待当前阶段及已接受命令完成。", "exit"),
}
ACTION_LABELS = {
    "retry": "重新处理", "ignore": "忽略", "link": "关联已有账单",
    "accept-source": "接纳来源", "confirm-new": "确认新建",
}


def scalar(value) -> str:
    if value is None:
        return "未提供"
    if not isinstance(value, (str, int, float, Decimal, date, datetime)):
        return "未提供"
    # Text 禁用 Rich markup；同时去掉数据中的终端控制字符，保留可读换行。
    return "".join(c if c.isprintable() or c == "\n" else "�" for c in str(value))


def text(value, style="") -> Text:
    return Text(scalar(value), style=style, overflow="fold")


def label(mapping, value) -> str:
    return mapping.get(value, scalar(value))


def table(title, *columns) -> Table:
    result = Table(title=Text(title), box=box.SIMPLE, show_lines=False, expand=True)
    for column in columns:
        result.add_column(column, overflow="fold")
    return result


def help_text(command=None) -> str:
    commands = COMMANDS if command is None else {command: COMMANDS[command]}
    return "\n\n".join(
        f"{name} · {title}\n{description}\n用法：\n{example}"
        for name, (title, description, example) in commands.items()
    )


def render_help(console: Console, command=None):
    if command is not None:
        console.print(text(help_text(command)))
        return
    commands = table("控制台命令", "命令", "用途")
    for name, (title, _, _) in COMMANDS.items():
        commands.add_row(text(name, "cyan"), text(title))
    console.print(commands)
    console.print(text("help 命令名 查看示例与影响，例如 help recheck。"))
    console.print(text("status 只读；issues 中选择处理动作、sync 和 recheck 会改变处理状态。"))
    console.print(text("exit、Ctrl+C 或 EOF 一起退出 console 和 worker。"))


def _counts(rows, state_key) -> str:
    if rows is None:
        return "未提供"
    if not rows:
        return "暂无记录"
    return "；".join(
        f"{label(STATE_LABELS, row[state_key])} {scalar(row.get('count'))}"
        for row in rows
    )


def _issue_title(group) -> str:
    if group.get("code") == "reconciliation":
        states = [group.get("match_status"), group.get("ledger_check_status")]
        problems = [s for s in states if s not in (None, "matched", "not_checked")]
        if problems:
            return " / ".join(label(STATE_LABELS, s) for s in problems)
    return label(ISSUE_LABELS, group.get("code"))


def _next_step(group) -> str:
    if group.get("code") == "write_unknown" or group.get("status") == "unknown":
        return "仅核实原写入结果，不能重发"
    if group.get("status") in ("booked", "ignored"):
        return "查看当前对象诊断，不安排重新创建"
    if group.get("status") in ("pending", "queued", "dispatching"):
        return "正在处理，查看后续日志"
    code = group.get("code")
    if code == "duplicate_candidates":
        return "处理远端候选后执行 recheck，一次复查"
    if code == "duplicate_check_failed":
        return "检查账本连接，修正查询失败后执行 recheck"
    if code == "source_acceptance":
        return "进入 issues 核实并接纳来源"
    if code == "reconciliation":
        if group.get("match_status") in ("missing_source_transaction", "missing_statement_evidence"):
            return "资料缺口，补充来源邮件后重新核对"
        return "检查核对详情；远端删除不会自动补建"
    if code == "download_failed":
        return "检查邮箱连接及采集日志"
    if group.get("entity_type") == "email":
        return "核实原件或修复解析后，再人工处理"
    return "查看对象详情，修正原因后人工处理"


def _issue_summary(console, groups):
    summary = table("问题分组", "原因 / 状态", "诊断 / 对象", "下一步")
    for group in groups:
        heading = f"{_issue_title(group)}\n{label(ENTITY_LABELS, group.get('entity_type'))}"
        if group.get("status"):
            heading += f" · {label(STATE_LABELS, group['status'])}"
        if console.width < 72:
            console.print(text(heading, "bold"))
            console.print(text(f"诊断 {group['count']} 条 / 对象 {group['object_count']} 个"))
            console.print(text("下一步：" + _next_step(group)))
            continue
        summary.add_row(text(heading), text(f"{group['count']} / {group['object_count']}"),
                        text(_next_step(group)))
    if console.width >= 72:
        console.print(summary)


def _status(console, result):
    counts = table("处理状态", "阶段", "数量")
    for title, key, state in (
        ("邮件采集", "email_source_item", "status"), ("邮件解析", "email", "parse_status"),
        ("交易导入", "bank_transactions", "import_status"),
    ):
        counts.add_row(text(title), text(_counts(result.get(key), state)))
    console.print(counts)
    tasks = result.get("background_task")
    if tasks:
        task_table = table("后台任务", "类型", "状态", "数量")
        for row in tasks:
            task_table.add_row(text(label(TASK_LABELS, row.get("task_type"))),
                               text(label(STATE_LABELS, row.get("status"))), text(row.get("count")))
        console.print(task_table)
    else:
        console.print(text("后台任务：" + ("暂无记录" if tasks == [] else "未提供")))
    checkpoints = result.get("email_sync_checkpoint")
    if checkpoints:
        scans = table("首次历史扫描", "文件夹", "进度", "最近扫描（含时区）")
        for row in checkpoints:
            complete = row.get("historical_complete")
            scans.add_row(text(row.get("folder")),
                          text("已完成" if complete is True else "未完成" if complete is False else None),
                          text(row.get("last_scanned_at")))
        console.print(scans)
    else:
        console.print(text("首次历史扫描：" + ("尚未建立记录" if checkpoints == [] else "未提供")))
    diagnostic_count = result.get("issues")
    if diagnostic_count == 0:
        console.print(text("当前没有问题诊断。", "green"))
    else:
        console.print(text(f"当前 {scalar(diagnostic_count)} 条诊断，涉及 "
                           f"{scalar(result.get('issue_object_count'))} 个业务对象；诊断不等于失败交易。"))
        if result.get("issue_groups"):
            _issue_summary(console, result["issue_groups"])
        console.print(text("输入 issues 查看问题；历史扫描完成不代表全部交易已入账。"))


def _fields(console, fields):
    if console.width < 60:
        for key, value in fields:
            console.print(text(key, "dim"))
            console.print(text(value))
        return
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(style="dim", ratio=1, overflow="fold")
    grid.add_column(ratio=4, overflow="fold")
    for key, value in fields:
        grid.add_row(text(key), text(value))
    console.print(grid)


def _detail_fields(issue):
    context = issue.get("context") or {}
    for key, title in (
        ("transaction_date", "交易日期"), ("merchant", "商户"), ("report_key", "报告 ID"),
        ("folder", "文件夹"), ("uid", "邮件 UID"), ("task_type", "任务类型"),
    ):
        if key in context:
            yield title, label(TASK_LABELS, context[key]) if key == "task_type" else context[key]
    if "original_amount" in context or "original_currency" in context:
        yield "银行原币金额", f"{scalar(context.get('original_amount'))} {scalar(context.get('original_currency'))}"
    detail = issue.get("detail")
    if isinstance(detail, str):
        yield "原因说明", detail
        return
    if not isinstance(detail, dict):
        yield "原因说明", None
        return
    for key, title in (("locator", "原件位置"), ("detail", "原因说明"),
                       ("reason", "原因说明"), ("error_type", "错误类型")):
        if key in detail and not isinstance(detail[key], (dict, list)):
            yield title, detail[key]
    if issue["code"] == "reconciliation":
        for key, title in (("statement_report_key", "月账单 ID"),
                           ("statement_row_key", "月账单行"), ("bank_transaction_id", "交易 ID")):
            if key in detail:
                yield title, detail[key]
        yield "银行匹配", label(STATE_LABELS, detail.get("match_status"))
        yield "账本核验", label(STATE_LABELS, detail.get("ledger_check_status"))
        for prefix, title in (("expected", "预期金额"), ("actual", "账本金额")):
            amount, currency = detail.get(prefix + "_amount"), detail.get(prefix + "_currency")
            yield title, None if amount is None and currency is None else f"{scalar(amount)} {scalar(currency)}"
        if detail.get("last_error"):
            yield "本轮核对错误", detail["last_error"]
        detail = detail.get("details") or {}
        for key, title in (("reason", "核对说明"), ("settlement_error", "结算问题"),
                           ("observed_ledger_transaction_id", "已观察账单 ID")):
            if key in detail:
                yield title, detail[key]
        differences = detail.get("differences") or {}
        for key, title in (("type", "交易类型"), ("time", "交易时间"),
                           ("sourceAccountId", "账本账户"), ("currency", "币种"), ("amount", "金额"),
                           ("destinationAccountId", "转入账户"),
                           ("destinationAmount", "转入金额（分）")):
            difference = differences.get(key)
            if isinstance(difference, dict):
                yield title + "差异", (f"预期 {scalar(difference.get('expected'))} / "
                                       f"实际 {scalar(difference.get('actual'))}")
        if "account_visibility" in differences:
            yield "账户状态", "账本账户缺失或已隐藏"
        if "destination_currency" in differences:
            yield "转入账户状态", "转入账户缺失、已隐藏或币种不符"
    candidates = detail.get("candidate_ids")
    if isinstance(candidates, list):
        yield "候选数", len(candidates)
        if candidates:
            yield "候选 ID", "\n".join(scalar(item) for item in candidates)


def _issues(console, result, args):
    count = len(result)
    objects = len({(item["entity_type"], item["entity_id"]) for item in result})
    filtered = args is not None and (args.entity_type is not None or args.entity_id is not None)
    if not count:
        console.print(text("没有符合筛选条件的问题。" if filtered else "当前没有问题诊断。", "green"))
        return
    console.print(text(f"{'筛选结果：' if filtered else '当前'} {count} 条诊断，涉及 {objects} 个业务对象。"
                       "同一对象可能有多条诊断。"))
    if not filtered:
        _issue_summary(console, issue_groups(result))
        console.print(text("详情：issues --entity-type bank_transactions；当前可筛选对象类型："))
        console.print(text("、".join(sorted({item["entity_type"] for item in result}))))
        console.print(text("定位对象：issues --entity-id ID；重复候选处理后执行 recheck。"))
        return
    for index, issue in enumerate(result, 1):
        console.print(text(f"\n{index}. {label(ISSUE_LABELS, issue['code'])}", "bold"))
        _fields(console, [
            ("对象类型", f"{label(ENTITY_LABELS, issue['entity_type'])} · {issue['entity_type']}"),
            ("完整 ID", issue["entity_id"]), ("状态", label(STATE_LABELS, issue.get("status"))),
            ("诊断代码", issue["code"]), ("决定版本", issue.get("version")),
            *_detail_fields(issue),
        ])
        group = issue_groups([issue])[0]
        console.print(text("下一步：" + _next_step(group)))


def render_result(console: Console, command: str, result, args=None):
    if command == "status":
        _status(console, result)
    elif command == "issues":
        _issues(console, result, args)
    elif command == "sync":
        console.print(text("同步请求已排队。" if result["queued"] else "同步请求已合并到现有待处理任务。"))
        if result.get("since") is not None or result.get("until") is not None:
            console.print(text(f"邮件接收日期：{scalar(result.get('since'))} 至 "
                               f"{scalar(result.get('until')) if result.get('until') else '不设上界'}（含边界）"))
        console.print(text("这表示已安排采集，尚不代表采集或入账完成；不会复查已暂停的重复候选。"))
    elif command == "recheck":
        _fields(console, [("已安排", result["scheduled"]),
                          ("已在处理", result["already_pending"]),
                          ("已跳过", result["skipped"])])
        console.print(text("已在处理的交易不会重复安排；已跳过的交易当前不满足复查条件。"))
        if result["scheduled"]:
            console.print(text("本次安排一次复查，复查尚未完成；worker 将使用已有分类重新查重，通过后可能入账。"
                               "仍重复或查询失败会再次暂停，不会自动再次复查。"))
        else:
            console.print(text("本次没有新增复查安排；可用 status 查看处理中任务，用 issues 查看当前问题。"))
    elif command == "resolve":
        if result.get("result") == "intent recorded; external outcome must be verified":
            console.print(text("处理意图已保存；现有写入结果仍待核实，不会重新发送。", "yellow"))
        else:
            action = result.get("action")
            console.print(text(f"处理决定已保存：{label(ACTION_LABELS, action)}。"))
            if action in ("retry", "confirm-new", "accept-source"):
                console.print(text("后续检查通过后可能入账；本次反馈不表示账本写入完成。"))
            elif action == "link":
                console.print(text("已保存与已有账单的关联，本次没有新建账单。"))
        if args is not None:
            _fields(console, [("对象类型", label(ENTITY_LABELS, args.entity_type)),
                              ("完整 ID", args.entity_id)])
    else:
        raise ValueError(f"unsupported presentation command: {command}")
