"""合成日志与受控命令；不读取开发环境、不连接服务。"""

import asyncio
from datetime import date
from io import StringIO
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from ezbookkeeping_importer.entrypoints import cli, console
from ezbookkeeping_importer.entrypoints.log_tail import LogNotice, LogTailer, MAX_LINE_BYTES


def event(name="worker_started", **fields):
    return (
        json.dumps(
            {"time": "2026-09-29T12:00:00+00:00", "level": "INFO", "event": name, **fields},
            ensure_ascii=False,
        ).encode()
        + b"\n"
    )


def records(items):
    return [item for item in items if isinstance(item, dict)]


def notices(items):
    return "\n".join(item.message for item in items if isinstance(item, LogNotice))


def screen():
    stream = StringIO()
    return Console(file=stream, width=200, color_system=None), stream


def test_parser_reuses_cli_parameters_and_preserves_quoted_reason():
    line = (
        "resolve bank_transactions abc --version 2 --action retry "
        '--reason "中文 核实理由" --account-id account-1'
    )
    interactive = console.parse_line(line, "synthetic.toml")
    ordinary = cli.parse_command(
        cli.build_parser(), ["--config", "synthetic.toml", *console.shlex.split(line)]
    )
    assert vars(interactive) == vars(ordinary)
    result = console.parse_line("sync --since 2026-09-01 --until 2026-09-02", "c.toml")
    assert result.since == date(2026, 9, 1)
    assert result.until == date(2026, 9, 2)


@pytest.mark.parametrize(
    "line",
    [
        "worker",
        "migrate",
        "doctor",
        "restore-audit",
        "console",
        "quit extra",
        "sync --since 2026-09-31",
        "sync --since 2026-09-02 --until 2026-09-01",
        'resolve "unfinished',
    ],
)
def test_console_rejects_invalid_commands_without_exiting(line):
    with pytest.raises(console.CommandInputError):
        console.parse_line(line, "synthetic.toml")


@pytest.mark.parametrize("line", ["help", "help resolve", "sync --help"])
def test_console_help_does_not_print_outside_output_proxy(line, capsys):
    with pytest.raises(console.CommandHelp) as result:
        console.parse_line(line, "synthetic.toml")
    assert "usage:" in str(result.value)
    assert capsys.readouterr() == ("", "")


def test_non_tty_fails_before_reading_config(monkeypatch, capsys):
    monkeypatch.setattr(console.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(console, "load_settings", lambda *a, **k: pytest.fail("loaded config"))
    assert console.run_console("does-not-exist.toml") == 2
    assert "单次命令" in capsys.readouterr().err


def test_tail_starts_at_end_and_waits_for_complete_utf8_line(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_bytes(event())
    tailer = LogTailer(path)
    assert tailer.poll() == []
    payload = event("collection_progress", folder="中文文件夹", processed=2, total=3)
    cut = payload.index("中".encode()) + 1
    with path.open("ab") as file:
        file.write(payload[:cut])
    assert tailer.poll() == []
    with path.open("ab") as file:
        file.write(payload[cut:])
    found = records(tailer.poll())
    assert len(found) == 1 and found[0]["folder"] == "中文文件夹"
    assert tailer.poll() == []
    tailer.close()


def test_tail_missing_file_is_visible_once_then_reads_created_file_from_start(tmp_path):
    path = tmp_path / "worker.jsonl"
    tailer = LogTailer(path)
    assert "尚不存在" in notices(tailer.poll())
    assert tailer.poll() == []
    path.write_bytes(event())
    found = tailer.poll()
    assert "已恢复" in notices(found)
    assert records(found)[0]["event"] == "worker_started"
    tailer.close()


def test_rename_drains_old_descriptor_and_starts_new_file(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.touch()
    tailer = LogTailer(path)
    tailer.poll()
    path.write_bytes(event("parse_started"))
    path.rename(tmp_path / "worker.jsonl.1")
    path.write_bytes(event("parse_completed"))
    found = tailer.poll()
    assert [item["event"] for item in records(found)] == ["parse_started", "parse_completed"]
    assert "已轮转" in notices(found)
    assert tailer.poll() == []
    tailer.close()


def test_rename_gap_keeps_old_descriptor_and_discards_incomplete_old_line(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.touch()
    tailer = LogTailer(path)
    tailer.poll()
    path.write_bytes(event() + b'{"event":')
    path.rename(tmp_path / "worker.jsonl.1")
    found = tailer.poll()
    assert len(records(found)) == 1 and "尚不存在" in notices(found)
    path.write_bytes(event("worker_stopped"))
    found = tailer.poll()
    assert "末行不完整" in notices(found)
    assert [item["event"] for item in records(found)] == ["worker_stopped"]
    tailer.close()


@pytest.mark.parametrize("grow_beyond_offset", [False, True])
def test_truncate_detected_even_after_file_regrows(tmp_path, grow_beyond_offset):
    path = tmp_path / "worker.jsonl"
    path.write_bytes(event())
    tailer = LogTailer(path, start_at_end=False)
    assert len(records(tailer.poll())) == 1
    content = event("worker_stopped")
    path.write_bytes(content * (3 if grow_beyond_offset else 1))
    found = tailer.poll()
    assert "已截断" in notices(found)
    assert len(records(found)) == (3 if grow_beyond_offset else 1)
    assert tailer.poll() == []
    tailer.close()


def test_bad_lines_and_private_fields_do_not_leak(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_bytes(
        b'not-json SECRET\n["SECRET"]\n'
        + event(token="SECRET", request={"secret": "SECRET"})
        + b"\xff\n"
    )
    tailer = LogTailer(path, start_at_end=False)
    found = tailer.poll()
    assert len(records(found)) == 1
    assert notices(found).count("格式无效") == 3
    assert "SECRET" not in str(found)
    tailer.close()


def test_oversized_incomplete_line_is_bounded_and_following_record_survives(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_bytes(b"X" * (MAX_LINE_BYTES + 100) + b"\n" + event())
    tailer = LogTailer(path, start_at_end=False)
    found = []
    for _ in range(8):
        found.extend(tailer.poll())
    assert notices(found).count("读取上限") == 1
    assert len(records(found)) == 1
    tailer.close()


def test_read_failure_visible_without_echoing_path_or_exception(tmp_path, monkeypatch):
    path = tmp_path / "worker.jsonl"
    tailer = LogTailer(path)
    original_open = Path.open

    def fail(*args, **kwargs):
        raise PermissionError("SECRET unsafe diagnostic")

    monkeypatch.setattr(Path, "open", fail)
    first = tailer.poll()
    assert "PermissionError" in notices(first) and "SECRET" not in notices(first)
    assert tailer.poll() == []
    monkeypatch.setattr(Path, "open", original_open)
    path.touch()
    assert "已恢复" in notices(tailer.poll())
    tailer.close()


def test_render_uses_event_contract_and_distinguishes_sync_merge():
    display, output = screen()
    console.render_log(display, records_from_bytes(event("sync_completed")), "INFO")
    console.render_log(display, records_from_bytes(event("future_event")), "INFO")
    console.render_result(display, "sync", {"queued": False, "since": None, "until": None})
    console.render_result(display, "resolve", {"result": "decision saved", "action": "retry"})
    value = output.getvalue()
    assert "邮件采集完成" in value and "未识别事件" in value
    assert "已合并" in value and "处理决定已保存" in value
    assert "入账成功" not in value and "重试成功" not in value


def records_from_bytes(payload):
    return json.loads(payload)


def test_command_runtime_lives_and_closes_in_execution_thread(monkeypatch):
    calls = []
    main_thread = threading.get_ident()

    class Runtime:
        def __init__(self, settings, **kwargs):
            calls.append(("open", threading.get_ident(), kwargs))
            self.store = object()

        def close(self):
            calls.append(("close", threading.get_ident(), None))

    monkeypatch.setattr(cli, "Runtime", Runtime)
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: object())
    monkeypatch.setattr(cli.maintenance, "status", lambda store: {"synthetic": True})
    display, output = screen()
    args = console.parse_line("status", "synthetic.toml")
    asyncio.run(console._execute(args, display, cli.execute_command))
    assert [item[0] for item in calls] == ["open", "close"]
    assert calls[0][1] == calls[1][1] != main_thread
    assert calls[0][2]["command"] == "status"
    assert "synthetic" in output.getvalue()


def test_command_failure_is_visible_safe_and_closes_runtime(monkeypatch):
    closed = []
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: object())
    monkeypatch.setattr(
        cli,
        "Runtime",
        lambda *a, **k: SimpleNamespace(store=object(), close=lambda: closed.append(True)),
    )

    def fail(store):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(cli.maintenance, "status", fail)
    display, output = screen()
    asyncio.run(
        console._execute(console.parse_line("status", "c.toml"), display, cli.execute_command)
    )
    assert closed == [True]
    assert "RuntimeError" in output.getvalue() and "SECRET" not in output.getvalue()


class QueueSession:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.prompts = 0

    async def prompt_async(self, prompt):
        self.prompts += 1
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def test_slow_command_does_not_block_help_logs_ctrl_c_or_quit(tmp_path):
    async def scenario():
        path = tmp_path / "worker.jsonl"
        path.touch()
        session = QueueSession()
        display, output = screen()
        started = threading.Event()
        release = threading.Event()
        executed = []

        def execute(args):
            executed.append(args.command)
            started.set()
            assert release.wait(3)
            return {"synthetic": True}

        runner = asyncio.create_task(
            console.interact("synthetic.toml", path, "INFO", session, display, execute=execute)
        )
        try:
            await wait_until(lambda: session.prompts == 1)
            session.queue.put_nowait("status")
            await wait_until(started.is_set)
            session.queue.put_nowait(KeyboardInterrupt())
            session.queue.put_nowait("help sync")
            session.queue.put_nowait("issues")
            await wait_until(lambda: "上一条命令仍在执行" in output.getvalue())
            with path.open("ab") as file:
                file.write(event("collection_progress", processed=3, total=5))
            await wait_until(lambda: "邮件采集进度" in output.getvalue())
            assert "--since" in output.getvalue()
            session.queue.put_nowait("quit")
            await wait_until(lambda: "正在等待" in output.getvalue())
            assert not runner.done()
            assert executed == ["status"]
            release.set()
            await asyncio.wait_for(runner, 3)
            assert "synthetic" in output.getvalue()
            assert "控制台已退出" in output.getvalue()
        finally:
            release.set()
            if not runner.done():
                session.queue.put_nowait("quit")
                await asyncio.wait_for(runner, 3)

    asyncio.run(scenario())


def test_real_prompt_session_handles_completion_history_chinese_and_ctrl_c(tmp_path):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    async def scenario(pipe):
        path = tmp_path / "worker.jsonl"
        path.touch()
        display, output = screen()
        session = PromptSession(
            input=pipe,
            output=DummyOutput(),
            completer=WordCompleter(["status", "issues", "resolve", "quit"]),
            history=InMemoryHistory(),
        )
        executed = []

        def execute(args):
            executed.append(args)
            return {"command_finished": len(executed)}

        runner = asyncio.create_task(
            console.interact("c.toml", path, "INFO", session, display, execute=execute)
        )
        try:
            await asyncio.sleep(0.05)
            pipe.send_text("sta")
            with path.open("ab") as file:
                file.write(event("collection_progress", processed=1, total=2))
            await wait_until(lambda: "邮件采集进度" in output.getvalue())
            pipe.send_text("\t\r")
            await wait_until(lambda: output.getvalue().count("command_finished") == 1)
            pipe.send_text("\x1b[A\r")
            await wait_until(lambda: output.getvalue().count("command_finished") == 2)
            pipe.send_text("resolve bank_transactions abc --action retry --reason 中文理由\r")
            await wait_until(lambda: output.getvalue().count("command_finished") == 3)
            pipe.send_text("do-not-run-this\x03")
            await asyncio.sleep(0.05)
            pipe.send_text("issues\r")
            await wait_until(lambda: output.getvalue().count("command_finished") == 4)
            pipe.send_text("quit\r")
            await asyncio.wait_for(runner, 3)
            assert [args.command for args in executed] == ["status", "status", "resolve", "issues"]
            assert executed[2].reason == "中文理由"
            assert "命令未接受" not in output.getvalue()
        finally:
            if not runner.done():
                pipe.send_text("\x03quit\r")
                await asyncio.wait_for(runner, 3)

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        asyncio.run(scenario(pipe))


def test_unexpected_tail_failure_interrupts_prompt_and_is_not_silent(tmp_path, monkeypatch):
    def fail(self):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(LogTailer, "poll", fail)

    async def scenario():
        display, _ = screen()
        with pytest.raises(console.ImporterError, match="日志跟随已中断（RuntimeError）") as error:
            await asyncio.wait_for(
                console.interact(
                    "c.toml", tmp_path / "worker.jsonl", "INFO", QueueSession(), display
                ),
                3,
            )
        assert "SECRET" not in str(error.value)

    asyncio.run(scenario())


def test_repeated_read_failure_does_not_claim_recovery_before_read_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "worker.jsonl"
    path.touch()
    tailer = LogTailer(path)
    tailer.poll()
    original_read = tailer._read

    def fail():
        raise OSError("SECRET")

    monkeypatch.setattr(tailer, "_read", fail)
    assert "读取失败" in notices(tailer.poll())
    assert tailer.poll() == []
    monkeypatch.setattr(tailer, "_read", original_read)
    assert "已恢复" in notices(tailer.poll())
    tailer.close()


def test_logging_failure_has_specific_safe_command_diagnostic():
    from ezbookkeeping_importer.domain.errors import LogPersistenceError

    error = cli.command_error(LogPersistenceError("SECRET"))
    assert "日志持久化失败" in error["message"]
    assert "SECRET" not in error["message"]


def test_unknown_write_resolution_feedback_only_reports_saved_intent():
    display, output = screen()
    console.render_result(
        display,
        "resolve",
        {"result": "intent recorded; external outcome must be verified", "status": "unknown"},
    )
    assert "处理意图已保存" in output.getvalue()
    assert "处理决定已保存" not in output.getvalue()


def test_old_log_events_keep_safe_location_fields(tmp_path):
    path = tmp_path / "worker.jsonl"
    path.write_bytes(
        event("sync_completed", job_id=12, source_row_id="transaction-1", target_id="ledger-1")
    )
    tailer = LogTailer(path, start_at_end=False)
    record = records(tailer.poll())[0]
    assert record["task_id"] == 12
    assert record["transaction_id"] == "transaction-1"
    assert record["ledger_transaction_id"] == "ledger-1"
    tailer.close()


def test_unexpected_command_render_failure_interrupts_idle_prompt(tmp_path, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic output failure")

    monkeypatch.setattr(console, "render_result", fail)

    async def scenario():
        path = tmp_path / "worker.jsonl"
        path.touch()
        session = QueueSession()
        session.queue.put_nowait("status")
        display, _ = screen()
        with pytest.raises(RuntimeError, match="synthetic output failure"):
            await asyncio.wait_for(
                console.interact(
                    "c.toml", path, "INFO", session, display, execute=lambda args: {}
                ),
                3,
            )

    asyncio.run(scenario())
