"""使用真实 spawn 与合成日志验证生命周期；不连接数据库或外部服务。"""

import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from rich.console import Console

from ezbookkeeping_importer.entrypoints import cli, console, run, worker
from ezbookkeeping_importer.domain.errors import ImporterError


def append_event(root, event):
    with (root / "worker.jsonl").open("a") as output:
        output.write(json.dumps({"level": "INFO", "event": event}) + "\n")


def synthetic_worker(config_path, stop_event, connection):
    root = Path(config_path)
    with worker.StopSignals(stop_event) as stopping:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
        (root / "pid").write_text(str(os.getpid()))
        append_event(root, "worker_starting")
        append_event(root, "worker_started")
        connection.send(("ready", None))
        while not stopping.is_set():
            time.sleep(0.01)
        time.sleep(0.1)  # 合成在途阶段，必须等它完成，不能直接 terminate。
        append_event(root, "worker_stopped")
        (root / "finished").touch()
    connection.close()


def startup_wait_worker(config_path, stop_event, connection):
    root = Path(config_path)
    (root / "pid").write_text(str(os.getpid()))
    stop_event.wait(5)
    (root / "finished").touch()
    connection.close()


def error_worker(config_path, stop_event, connection):
    connection.send(("error", cli.command_error(RuntimeError("SECRET"))))
    connection.close()
    raise SystemExit(1)


def early_exit_worker(config_path, stop_event, connection):
    connection.close()


def ready_exit_worker(config_path, stop_event, connection):
    connection.send(("ready", None))
    time.sleep(0.1)
    connection.close()


class Session:
    def __init__(self, *lines):
        self.lines = list(lines)
        self.prompts = 0

    async def prompt_async(self, prompt, **kwargs):
        self.prompts += 1
        if not self.lines:
            await asyncio.Future()
        item = self.lines.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def display():
    output = StringIO()
    return Console(file=output, width=200, color_system=None), output


def settings(root, level="INFO"):
    return SimpleNamespace(log_dir=root, log_level=level)


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def assert_finished(root):
    assert (root / "finished").exists()
    pid = int((root / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("exit_input", ["exit", KeyboardInterrupt(), EOFError()])
def test_exit_ctrl_c_and_eof_wait_for_owned_worker(tmp_path, exit_input):
    screen, output = display()
    asyncio.run(
        run.run_session(
            str(tmp_path), settings(tmp_path), Session(exit_input), screen,
            worker_target=synthetic_worker,
        )
    )
    assert_finished(tmp_path)
    assert "等待当前阶段结束" in output.getvalue()
    assert "console 和 worker 已退出" in output.getvalue()
    assert output.getvalue().count("正在退出") == 1


@pytest.mark.parametrize("existing", [False, True])
def test_startup_log_boundary_no_missing_or_duplicate_events(tmp_path, existing):
    if existing:
        append_event(tmp_path, "parse_started")
    screen, output = display()
    asyncio.run(
        run.run_session(
            str(tmp_path), settings(tmp_path), Session("exit"), screen,
            worker_target=synthetic_worker,
        )
    )
    for event in ("worker_starting", "worker_started", "worker_stopped"):
        assert output.getvalue().count(console.EVENT_LABELS[event]) == 1
    assert console.EVENT_LABELS["parse_started"] not in output.getvalue()


def test_error_log_level_does_not_gate_ready(tmp_path):
    screen, output = display()
    session = Session("exit")
    asyncio.run(
        run.run_session(
            str(tmp_path), settings(tmp_path, "ERROR"), session, screen,
            worker_target=synthetic_worker,
        )
    )
    assert session.prompts == 1
    assert "worker 已就绪" in output.getvalue()
    assert console.EVENT_LABELS["worker_starting"] not in output.getvalue()
    assert_finished(tmp_path)


@pytest.mark.parametrize("target", [error_worker, early_exit_worker, ready_exit_worker])
def test_worker_failure_and_unexpected_zero_exit_fail_owner(tmp_path, target):
    screen, output = display()
    with pytest.raises(ImporterError) as error:
        asyncio.run(
            run.run_session(str(tmp_path), settings(tmp_path), Session(), screen, worker_target=target)
        )
    assert "worker" in str(error.value)
    assert "SECRET" not in str(error.value) + output.getvalue()
    assert "console 和 worker 已退出" not in output.getvalue()
    assert "运行已中断" in output.getvalue()


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_repeated_signal_during_startup_waits_without_ready(tmp_path, sig):
    async def scenario():
        screen, output = display()
        session = Session()
        task = asyncio.create_task(
            run.run_session(
                str(tmp_path), settings(tmp_path), session, screen,
                worker_target=startup_wait_worker,
            )
        )
        await until(lambda: (tmp_path / "pid").exists())
        os.kill(os.getpid(), sig)
        os.kill(os.getpid(), sig)
        await task
        assert session.prompts == 0
        assert "worker 已就绪" not in output.getvalue()
        assert output.getvalue().count("正在退出") == 1
        assert_finished(tmp_path)
    previous = signal.getsignal(sig)
    asyncio.run(scenario())
    assert signal.getsignal(sig) is previous


def test_parent_console_failure_reaps_worker(tmp_path, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("synthetic console failure")

    monkeypatch.setattr(console, "interact", broken)
    screen, _ = display()
    with pytest.raises(RuntimeError, match="synthetic console failure"):
        asyncio.run(
            run.run_session(
                str(tmp_path), settings(tmp_path), Session(), screen,
                worker_target=synthetic_worker,
            )
        )
    assert_finished(tmp_path)


def test_stop_request_precedes_wait_for_accepted_command(tmp_path, monkeypatch):
    async def scenario():
        screen, output = display()
        release = threading.Event()
        started = threading.Event()
        original = console.interact

        def execute(args):
            started.set()
            assert release.wait(5)
            return {"finished_command": True}

        async def interact(*args, **kwargs):
            await original(*args, **kwargs, execute=execute)

        monkeypatch.setattr(console, "interact", interact)
        session = Session("status")
        task = asyncio.create_task(
            run.run_session(
                str(tmp_path), settings(tmp_path), session, screen,
                worker_target=synthetic_worker,
            )
        )
        try:
            await until(started.is_set)
            os.kill(os.getpid(), signal.SIGINT)
            await until(lambda: (tmp_path / "finished").exists())
            assert not task.done()
            assert "等待已接受" in output.getvalue()
            release.set()
            await task
            assert "finished_command" in output.getvalue()
            assert_finished(tmp_path)
        finally:
            release.set()
            await task
    asyncio.run(scenario())


def test_launcher_uses_own_root_and_preserves_argument_boundaries(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    launcher = root / "run"
    launcher.write_bytes(Path("run").read_bytes())
    launcher.chmod(0o755)
    commands = tmp_path / "bin"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
    uv.chmod(0o755)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]}
    default = subprocess.run(
        [str(launcher)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5, check=True
    )
    assert default.stdout.splitlines() == [str(root), "run", "--env-file", ".env", "ebki", "run"]
    args = ["--config", "other config.toml", "resolve", "email", "one", "--reason", "中文 理由"]
    explicit = subprocess.run(
        [str(launcher), *args], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=5, check=True
    )
    assert explicit.stdout.splitlines()[5:] == args


@pytest.mark.parametrize("command", ["console", "quit"])
def test_removed_cli_commands_are_rejected(command):
    with pytest.raises(SystemExit) as error:
        cli.parse_command(cli.build_parser(), [command])
    assert error.value.code == 2
    with pytest.raises(console.CommandInputError):
        console.parse_line(command, "config.toml")


def test_log_reader_failure_reaps_already_started_worker(tmp_path, monkeypatch):
    original = run.LogTailer.poll

    def broken(tailer):
        if (tmp_path / "pid").exists():
            raise ImporterError("synthetic log read failure")
        return original(tailer)

    monkeypatch.setattr(run.LogTailer, "poll", broken)
    screen, _ = display()
    with pytest.raises(ImporterError):
        asyncio.run(
            run.run_session(
                str(tmp_path), settings(tmp_path), Session(), screen,
                worker_target=synthetic_worker,
            )
        )
    assert_finished(tmp_path)


def test_simultaneous_parent_child_sigint_is_graceful(tmp_path):
    async def scenario():
        screen, output = display()
        task = asyncio.create_task(
            run.run_session(
                str(tmp_path), settings(tmp_path), Session(), screen,
                worker_target=synthetic_worker,
            )
        )
        await until(lambda: (tmp_path / "pid").exists())
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(int((tmp_path / "pid").read_text()), signal.SIGINT)
        await task
        assert_finished(tmp_path)
        assert "意外退出" not in output.getvalue()
        assert output.getvalue().count("正在退出") == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("boundary", ["before_block", "while_blocked"])
def test_stop_received_before_spawn_does_not_start_worker(tmp_path, monkeypatch, sig, boundary):
    screen, output = display()
    original_print = screen.print
    original_mask = signal.pthread_sigmask

    def stop_on_starting(*args, **kwargs):
        original_print(*args, **kwargs)
        if boundary == "before_block" and args == ("正在启动 worker…",):
            os.kill(os.getpid(), sig)

    def stop_while_blocked(how, mask):
        previous = original_mask(how, mask)
        if boundary == "while_blocked" and how == signal.SIG_BLOCK:
            os.kill(os.getpid(), sig)
        return previous

    monkeypatch.setattr(screen, "print", stop_on_starting)
    monkeypatch.setattr(signal, "pthread_sigmask", stop_while_blocked)
    asyncio.run(
        run.run_session(
            str(tmp_path), settings(tmp_path), Session(), screen,
            worker_target=synthetic_worker,
        )
    )

    assert not (tmp_path / "pid").exists()
    assert not (tmp_path / "worker.jsonl").exists()
    assert "worker 已就绪" not in output.getvalue()
    assert "console 和 worker 已退出" in output.getvalue()


def test_spawn_failure_is_reported_and_restores_signal_state(tmp_path, monkeypatch):
    screen, _ = display()
    previous_handler = signal.getsignal(signal.SIGINT)
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    def fail_start(process):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(run.multiprocessing.get_context("spawn").Process, "start", fail_start)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        asyncio.run(
            run.run_session(str(tmp_path), settings(tmp_path), Session(), screen)
        )

    assert signal.getsignal(signal.SIGINT) is previous_handler
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == previous_mask
