"""交互使用真实 PromptSession，业务动作通过受控执行器验证。"""

import argparse
import asyncio
from io import StringIO
import threading

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from ezbookkeeping_importer.entrypoints import cli, console, issue_flow
from ezbookkeeping_importer.application.issue_interaction import resolution_actions
from ezbookkeeping_importer.domain.errors import Conflict


def item():
    return {
        "entity_type": "bank_transactions",
        "entity_id": "synthetic",
        "version": 3,
        "code": "duplicate_candidates",
        "detail": {"candidate_ids": ["a"]},
        "status": "issue",
        "context": {"merchant": "中文商户", "original_amount": "12.30", "original_currency": "CNY"},
    }


async def direct(awaitable):
    return await awaitable


def display():
    stream = StringIO()
    return Console(file=stream, width=120, color_system=None), stream


@pytest.mark.parametrize("keys,result", [("\x1b[B\r", 1), ("\x1b[A\r", 56), ("\x1b", None)])
def test_real_arrow_selection_and_escape_across_large_group(keys, result):
    async def run(pipe):
        session = PromptSession(input=pipe, output=DummyOutput())
        task = asyncio.create_task(
            issue_flow.choose(session, "待处理", [(i, f"商户 {i}") for i in range(57)])
        )
        await asyncio.sleep(0.03)
        pipe.send_text(keys)
        assert await asyncio.wait_for(task, 2) == result

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        asyncio.run(run(pipe))


@pytest.mark.parametrize("key", ["\x03", "\x04"])
def test_navigation_ctrl_c_and_eof_exit(key):
    async def run(pipe):
        task = asyncio.create_task(
            issue_flow.choose(
                PromptSession(input=pipe, output=DummyOutput()), "选择", [(None, "返回")]
            )
        )
        await asyncio.sleep(0.03)
        pipe.send_text(key)
        with pytest.raises(EOFError):
            await asyncio.wait_for(task, 2)

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        asyncio.run(run(pipe))


@pytest.mark.parametrize("interactive", [True, False])
def test_resolve_is_not_a_public_command(interactive):
    with pytest.raises(SystemExit):
        cli.parse_command(cli.build_parser(interactive=interactive), ["resolve", "--help"])


def test_single_recheck_passes_selected_snapshot_without_reclassifying():
    async def run():
        screen, stream = display()
        calls = []
        selected = item()

        def execute(args):
            calls.append(args)
            if args.command == "recheck":
                return {"scheduled": 1, "already_pending": 0, "skipped": 0}
            return {
                "issue": selected,
                "actions": ["recheck", "ignore"],
                "decision": {},
                "active": False,
            }

        flow = issue_flow.IssueFlow("test", None, screen, execute, direct)
        choices = iter(["recheck", True, None])

        async def menu(*args):
            return next(choices)

        flow.menu = menu
        assert await flow.object(selected)
        assert calls[-1].command == "recheck"
        assert calls[-1].targets == [selected]
        assert "本次安排一次复查" in stream.getvalue()

    asyncio.run(run())


def test_cancel_human_decision_does_not_mutate():
    async def run():
        screen, _ = display()
        calls = []

        def execute(args):
            calls.append(args)
            return {"actions": ["ignore"], "decision": {}, "active": False}

        flow = issue_flow.IssueFlow("test", None, screen, execute, direct)
        choices = iter(["ignore", None, None])

        async def menu(*args):
            return next(choices)

        flow.menu = menu
        assert not await flow.object(item())
        assert len(calls) == 1 and calls[0].operation == "detail"

    asyncio.run(run())


def test_conflict_refreshes_without_replaying_old_selection():
    async def run():
        screen, stream = display()
        calls = []

        def execute(args):
            calls.append(args)
            if getattr(args, "operation", None):
                raise Conflict("changed")
            return [item()]

        flow = issue_flow.IssueFlow("test", None, screen, execute, direct)
        choices = iter([("bank_transactions", "duplicate_candidates", "issue"), 0, None])

        async def menu(*args):
            return next(choices)

        flow.menu = menu
        await flow.run(argparse.Namespace(entity_type=None, entity_id=None))
        assert len(calls) == 3
        assert "未重放" in stream.getvalue()

    asyncio.run(run())


def test_stopping_waits_for_accepted_action_without_cancelling_thread():
    async def run():
        screen, _ = display()
        stopping = asyncio.Event()
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        def execute(args):
            started.set()
            assert release.wait(2)
            completed.set()
            return {"result": "decision saved"}

        async def wait(awaitable):
            return await console._navigation_wait(awaitable, (), stopping)

        flow = issue_flow.IssueFlow("test", None, screen, execute, wait)
        task = asyncio.create_task(flow.call("issues", operation="resolve"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        stopping.set()
        await asyncio.sleep(0.03)
        assert not task.done()
        release.set()
        with pytest.raises(EOFError):
            await task
        assert completed.is_set()

    asyncio.run(run())


def test_unknown_and_booked_actions_do_not_offer_creation():
    row = {"ledger_transaction_id": None, "import_decision": {"payload": {"type": 3}}}
    assert resolution_actions("bank_transactions", row, active=True) == ["retry"]
    assert resolution_actions("bank_transactions", {**row, "ledger_transaction_id": "1"}) == []
    assert resolution_actions("bank_statement_reconciliation", {}) == []


def test_comparison_displays_transfer_currency_and_missing_target():
    screen, stream = display()
    issue_flow.comparison(
        screen,
        {
            "accounts": [
                {"id": "a", "name": "人民币", "currency": "CNY"},
                {"id": "b", "name": "美元", "currency": "USD"},
            ],
            "categories": [{"id": "c", "path": "其他 → 转账"}],
            "decision": {
                "payload": {
                    "type": 4,
                    "sourceAccountId": "a",
                    "destinationAccountId": "b",
                    "sourceAmount": 12300,
                    "destinationAmount": 1700,
                    "categoryId": "c",
                }
            },
            "candidates": [{"id": "missing\x1b[2J", "transaction": None}],
        },
    )
    value = stream.getvalue()
    assert "123 CNY" in value and "17 USD" in value
    assert "远端已不存在" in value and "其他 → 转账" in value
    assert "\x1b" not in value


def test_menu_restores_main_prompt_bindings_completion_and_chinese_input():
    from prompt_toolkit.completion import WordCompleter

    async def run(pipe):
        completer = WordCompleter(["status"])
        session = PromptSession(input=pipe, output=DummyOutput(), completer=completer)
        task = asyncio.create_task(issue_flow.choose(session, "菜单", [(None, "返回")]))
        await asyncio.sleep(0.03)
        pipe.send_text("\r")
        await task
        assert session.key_bindings is None and session.completer is completer
        task = asyncio.create_task(session.prompt_async("ebki> ", handle_sigint=False))
        await asyncio.sleep(0.03)
        pipe.send_text("中文\r")
        assert await task == "中文"

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        asyncio.run(run(pipe))


def test_public_help_does_not_advertise_removed_resolve():
    from ezbookkeeping_importer.entrypoints.presentation import render_help
    screen, stream = display()
    render_help(screen)
    assert "resolve" not in stream.getvalue()
    assert "issues 中选择处理动作" in stream.getvalue()


def test_monitor_failure_leaves_flow_without_accepting_another_query():
    async def run():
        screen, stream = display()
        calls = []

        def execute(args):
            calls.append(args)
            return [item()]

        flow = issue_flow.IssueFlow("test", None, screen, execute, direct)

        async def menu(*args):
            return ("bank_transactions", "duplicate_candidates", "issue")

        async def group(*args):
            watcher = asyncio.create_task(asyncio.sleep(0))
            await watcher
            await console._navigation_wait(asyncio.sleep(0), [watcher], asyncio.Event())

        flow.menu, flow.group = menu, group
        with pytest.raises(console.ConsoleMonitorError):
            await flow.run(argparse.Namespace(entity_type=None, entity_id=None))
        assert len(calls) == 1
        assert "操作失败" not in stream.getvalue()

    asyncio.run(run())


def test_narrow_menu_keeps_long_labels_within_one_terminal_line():
    from types import SimpleNamespace
    from rich.cells import cell_len

    class Session:
        key_bindings = None
        completer = None
        app = SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(rows=15, columns=32))
        )

        async def prompt_async(self, message, **kwargs):
            rows = message()
            assert all(cell_len(text.rstrip("\n")) < 32 for _, text in rows)
            assert len(rows) <= 15
            assert any("…" in text for _, text in rows)
            return None

    asyncio.run(issue_flow.choose(Session(), "很长的菜单标题" * 20, [(i, "商户" * 100) for i in range(57)]))


def test_navigation_normal_shutdown_monitor_is_not_a_failure():
    async def run():
        stopping = asyncio.Event()
        stopping.set()
        watcher = asyncio.create_task(stopping.wait())
        await watcher
        with pytest.raises(EOFError):
            await console._navigation_wait(asyncio.sleep(0), [watcher], stopping)

    asyncio.run(run())
