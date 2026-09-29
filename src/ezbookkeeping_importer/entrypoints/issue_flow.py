"""单个 PromptSession 上的问题导航；没有独立输入线程或账务写入路径。"""

import argparse
import asyncio
from decimal import Decimal
from datetime import datetime

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import FormattedText
from rich.table import Table
from rich.text import Text

from ..application.recheck import RECHECK_CODES
from ..domain.errors import Conflict
from . import cli
from .console import ConsoleMonitorError
from .presentation import ISSUE_LABELS, ACTION_LABELS, STATE_LABELS, scalar, render_result


async def navigation_prompt(session, message, bindings):
    previous_bindings, previous_completer = session.key_bindings, session.completer
    session.completer = None
    try:
        return await session.prompt_async(
            message, key_bindings=bindings, default="", handle_sigint=False
        )
    finally:
        # PromptSession retains per-call bindings; restore the ordinary command input.
        session.key_bindings, session.completer = previous_bindings, previous_completer


async def choose(session, title, options):
    selected = 0
    bindings = KeyBindings()

    @bindings.add("up")
    def up(event):
        nonlocal selected
        selected = (selected - 1) % len(options)

    @bindings.add("down")
    def down(event):
        nonlocal selected
        selected = (selected + 1) % len(options)

    @bindings.add("enter")
    def accept(event):
        event.app.exit(result=options[selected][0])

    @bindings.add("escape")
    def back(event):
        event.app.exit(result=None)

    @bindings.add("c-c")
    @bindings.add("c-d")
    def stop(event):
        event.app.exit(exception=EOFError())

    @bindings.add("<any>")
    def ignore_typing(event):
        pass

    def prompt():
        height = max(3, min(10, session.app.output.get_size().rows - 7))
        width = max(1, session.app.output.get_size().columns - 1)

        def line(value):
            text = Text(scalar(value).replace("\n", " "))
            text.truncate(width, overflow="ellipsis")
            return text.plain + "\n"

        start = max(0, min(selected - height // 2, len(options) - height))
        visible = options[start : start + height]
        rows = [("", line(title))]
        rows.append(("", f"第 {selected + 1}/{len(options)} 项\n"))
        for index, (_, label) in enumerate(visible, start):
            rows.append(
                (
                    "reverse" if index == selected else "",
                    line(("❯ " if index == selected else "  ") + scalar(label)),
                )
            )
        rows.append(("", line("↑↓ 选择 · Enter 确认 · Esc 返回 · Ctrl+C 退出项目")))
        return FormattedText(rows)

    return await navigation_prompt(session, prompt, bindings)


async def reason(session, title="处理理由（Esc 取消）："):
    bindings = KeyBindings()

    @bindings.add("escape")
    def back(event):
        event.app.exit(result="")

    return await navigation_prompt(session, title, bindings)


def summary(item):
    context = item.get("context") or {}
    return " · ".join(
        scalar(v)
        for v in (
            context.get("transaction_date") or context.get("report_date") or "",
            context.get("merchant") or context.get("folder") or item["entity_id"],
            context.get("original_amount", ""),
            context.get("original_currency", ""),
        )
        if v != ""
    )


def comparison(console, data):
    accounts = {str(a["id"]): a for a in data["accounts"]}
    categories = {str(c["id"]): c.get("path", c.get("name", c["id"])) for c in data["categories"]}
    table = Table(title="账单对比（远端实时读取；缺失不代表可以直接新建）")
    for name in ("对象", "时间", "金额", "账户", "分类", "商户 / 备注"):
        table.add_column(name, overflow="fold")
    rows = [("本地交易", data["decision"].get("payload"))]
    rows += [("候选 " + c["id"], c["transaction"]) for c in data["candidates"]]
    for label, row in rows:
        if row is None:
            table.add_row(Text(scalar(label)), Text("远端已不存在"))
            continue
        when = (
            datetime.fromtimestamp(row["time"]).astimezone().isoformat()
            if row.get("time")
            else "未提供"
        )
        amount = str(Decimal(str(row["sourceAmount"])) / 100) if "sourceAmount" in row else "未提供"
        source = accounts.get(str(row.get("sourceAccountId")), {})
        account = source.get("name", row.get("sourceAccountId"))
        amount += " " + str(source.get("currency", "币种未提供"))
        if row.get("type") == 4:
            destination = accounts.get(str(row.get("destinationAccountId")), {})
            account = f"{account} → {destination.get('name', row.get('destinationAccountId'))}"
            amount += (
                " → "
                + str(Decimal(str(row["destinationAmount"])) / 100)
                + " "
                + str(destination.get("currency", "币种未提供"))
            )
        table.add_row(
            *[
                Text(scalar(v))
                for v in (
                    label,
                    when,
                    amount,
                    account,
                    categories.get(str(row.get("categoryId")), row.get("categoryId")),
                    row.get("comment"),
                )
            ]
        )
    console.print(table)


class IssueFlow:
    def __init__(self, config, session, console, execute, wait):
        self.config, self.session, self.console = config, session, console
        self.execute, self.wait = execute, wait

    async def menu(self, title, options):
        return await self.wait(choose(self.session, title, options))

    async def call(self, command, **kwargs):
        args = argparse.Namespace(
            config=self.config, command=command, entity_type=None, entity_id=None
        )
        for key, value in kwargs.items():
            setattr(args, key, value)
        task = asyncio.create_task(asyncio.to_thread(self.execute, args))
        # Cancellation stops navigation, never abandons a command already accepted.
        try:
            return await self.wait(asyncio.shield(task))
        finally:
            if not task.done():
                self.console.print("正在等待已接受的命令完成…")
            await task

    async def run(self, filters):
        while True:
            items = await self.call(
                "issues", entity_type=filters.entity_type, entity_id=filters.entity_id
            )
            groups: dict[tuple, list] = {}
            for item in items:
                groups.setdefault((item["entity_type"], item["code"], item["status"]), []).append(
                    item
                )
            options = [
                (
                    key,
                    f"{ISSUE_LABELS.get(key[1], key[1])} · {STATE_LABELS.get(key[2], key[2])} · {len(group)} {'笔' if key[0] == 'bank_transactions' else '项'}",
                )
                for key, group in groups.items()
            ]
            key = await self.menu(
                "待处理问题" if items else "当前没有待处理问题",
                [*options, ("refresh", "刷新"), (None, "返回控制台")],
            )
            if key is None:
                return
            if key == "refresh":
                continue
            try:
                await self.group(groups[key])
            except (KeyboardInterrupt, EOFError, ConsoleMonitorError):
                raise
            except Conflict:
                self.console.print("状态已变化，已刷新；请重新选择，未重放刚才的操作。")
            except Exception as exc:
                self.console.print(
                    Text("操作失败：" + cli.command_error(exc)["message"], style="red")
                )

    async def group(self, items):
        while True:
            options: list[tuple] = [(index, summary(item)) for index, item in enumerate(items)]
            if all(
                i["entity_type"] == "bank_transactions"
                and i["code"] in RECHECK_CODES
                and i["status"] == "issue"
                for i in items
            ):
                options.insert(0, ("all", f"全部重新查重（仅本组 {len(items)} 笔）"))
            selected = await self.menu(
                "选择处理对象", [*options, ("refresh", "刷新"), (None, "返回")]
            )
            if selected is None or selected == "refresh":
                return
            if selected == "all":
                if await self.menu(
                    f"安排本组选中的 {len(items)} 笔复查；通过后可能入账",
                    [(None, "取消"), (True, "确认安排一次")],
                ):
                    result = await self.call("recheck", targets=items)
                    render_result(self.console, "recheck", result)
                    await self.menu("处理结果已显示；刷新查看后续进度", [(None, "刷新问题")])
                    return
            else:
                if await self.object(items[selected]):
                    return

    async def object(self, item):
        detail = await self.call("issues", operation="detail", selected=item)
        render_result(
            self.console,
            "issues",
            [item],
            argparse.Namespace(entity_type=item["entity_type"], entity_id=item["entity_id"]),
        )
        category = detail["decision"].get("classification") or {}
        if detail["decision"].get("payload"):
            category_id = str(detail["decision"]["payload"].get("categoryId"))
            paths = {
                str(c["id"]): c["path"]
                for c in category.get("audit", {}).get("category_snapshot", [])
            }
            name = paths.get(category_id)
            if name is None and category.get("classification_status") == "unmatched":
                name = "其他杂项 → 待分类"
            self.console.print(
                Text(
                    "分类："
                    + scalar(name or f"分类名称未缓存（{category_id}）")
                    + " · "
                    + scalar(category.get("reason"))
                )
            )
        actions = [
            (a, "重新查重" if a == "recheck" else ACTION_LABELS.get(a, a))
            for a in detail["actions"]
        ]
        if detail["active"] or item["status"] in {"unknown", "dispatching"}:
            actions = [(a, "记录核实请求（不重新发送）") for a, _ in actions]
        if (
            item["entity_type"] == "bank_transactions"
            and any(a in detail["actions"] for a in ("retry", "link"))
            and detail["decision"].get("payload")
            and not detail["active"]
        ):
            actions.append(("account", "修正账户并重新处理"))
        views = [("view", "查看候选账单与本地对比")] if detail["decision"].get("payload") else []
        if not actions and item["status"] in {"pending", "queued"}:
            self.console.print("已安排，等待 worker 处理；请刷新查看进度。")
        elif not actions:
            self.console.print("此项只供核对：请补充来源资料或在 ezBookkeeping 修正已入账账单。")
        while True:
            action = await self.menu("选择操作", views + actions + [(None, "返回")])
            if action is None:
                return False
            target = None
            account_id = None
            correcting_account = action == "account"
            if action in {"view", "link", "confirm-new", "account"}:
                data = await self.call("issues", operation="candidates", selected=item)
                comparison(self.console, data)
                if action == "view":
                    continue
                if action == "account":
                    account_id = await self.menu(
                        "选择正确账户（提交时校验币种及可用性）",
                        [
                            (str(a["id"]), f"{a.get('name')} · {a.get('currency')}")
                            for a in data["accounts"]
                        ]
                        + [(None, "取消")],
                    )
                    if account_id is None:
                        continue
                    action = "retry"
                if action == "link":
                    target = await self.menu(
                        "选择关联账单",
                        [(c["id"], c["id"]) for c in data["candidates"] if c["transaction"]]
                        + [("other", "输入其他已有账单 ID…"), (None, "取消")],
                    )
                    if target == "other":
                        target = (
                            await self.wait(reason(self.session, "已有账单 ID（Esc 取消）："))
                        ).strip()
                        if target:
                            data = await self.call(
                                "issues", operation="candidates", selected=item, target_id=target
                            )
                            comparison(self.console, data)
                            if not data["candidates"][0]["transaction"]:
                                self.console.print("账单不存在，请重新选择。")
                                continue
                    if not target:
                        continue
            effect = {
                "recheck": "仅安排一次查重，复用分类；通过后可能入账",
                "retry": "重新处理；可能重新分类并入账，未决写入只记录核实意图",
                "confirm-new": "确认是另一笔交易，允许越过重复候选检查并新建账单",
                "ignore": "忽略此对象，不再按原流程处理",
                "accept-source": "接纳此来源，允许继续解析和后续入账",
                "link": "关联已有账单，不新建；仍需通过金额、账户和时间校验",
            }[action]
            if item["entity_type"] == "email":
                effect += "；作用于整封邮件及其全部解析诊断"
            if not await self.menu(effect, [(None, "取消"), (True, "继续")]):
                continue
            note = "控制台发起一次重新检查"
            if action not in {"retry", "recheck"} or correcting_account:
                note = await self.wait(reason(self.session))
                if not note.strip():
                    continue
            if action == "recheck":
                result = await self.call("recheck", targets=[item])
                render_result(self.console, "recheck", result)
            else:
                result = await self.call(
                    "issues",
                    operation="resolve",
                    entity_type=item["entity_type"],
                    entity_id=item["entity_id"],
                    version=item["version"],
                    action=action,
                    reason=note,
                    code=item["code"],
                    target_id=target,
                    account_id=account_id,
                    selected=item,
                )
                render_result(self.console, "resolve", result, argparse.Namespace(action=action))
            await self.menu("处理结果已显示；刷新查看后续进度", [(None, "刷新问题")])
            return True
