"""前台运行所有者：托管唯一子 worker，等待它收尾后关闭交互终端。"""

import asyncio
import multiprocessing
import signal
import sys
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import StdoutProxy
from rich.console import Console

from ..bootstrap import Runtime
from ..config import load_settings
from ..domain.errors import ImporterError
from . import cli, console, worker
from .log_tail import LogTailer

MONITOR_SECONDS = 0.05


def worker_process(config_path, stop_event, status_connection):
    """spawn 目标只传配置路径和同步通道，不继承 Runtime 或连接。"""
    try:
        with worker.StopSignals(stop_event) as stopping:
            # 父进程在 spawn 时临时屏蔽信号，子进程装好处理器后再恢复。
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})
            if stopping.is_set():
                return
            settings = load_settings(config_path, command="worker")
            runtime = Runtime(settings, command="worker")
            try:
                worker.run(
                    runtime,
                    stop_event=stopping,
                    ready=lambda: status_connection.send(("ready", None)),
                    terminal=False,
                )
            finally:
                runtime.close()
    except Exception as exc:
        status_connection.send(("error", cli.command_error(exc)))
        raise SystemExit(1) from None
    finally:
        status_connection.close()


async def _watch_worker(process, connection, ready, stopping, signals):
    channel_open = True
    while True:
        while channel_open and connection.poll():
            try:
                kind, detail = connection.recv()
            except EOFError:
                channel_open = False
                break
            if kind == "ready":
                ready.set()
            elif kind == "error":
                raise ImporterError(
                    f"worker 失败（{detail['error_type']}）：{detail['message']}"
                )
            else:
                raise ImporterError("worker 控制通道收到未知状态")
        if process.exitcode is not None:
            if channel_open and connection.poll():
                continue
            # 父子同时收到终端信号时，子进程可能先结束，不能将正常取消误报崩溃。
            if signals.is_set():
                stopping.set()
            if process.exitcode != 0 or not stopping.is_set():
                raise ImporterError(f"worker 意外退出（退出码 {process.exitcode}）")
            return
        await asyncio.sleep(MONITOR_SECONDS)


async def _observe(awaitable, stopping, display):
    try:
        return await awaitable
    except Exception as exc:
        stopping.set()
        detail = cli.command_error(exc)
        display.print(f"运行已中断，正在收尾（{detail['error_type']}）：{detail['message']}")
        raise


async def _watch_stop(signals, stopping, child_stop, display):
    while not stopping.is_set():
        if signals.is_set():
            stopping.set()
            break
        await asyncio.sleep(MONITOR_SECONDS)
    child_stop.set()
    display.print("正在退出，等待当前阶段结束及已接受的命令完成；日志继续显示…")


async def _wait_ready(ready, stopping, watchers):
    ready_wait = asyncio.create_task(ready.wait())
    stop_wait = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait(
            {ready_wait, stop_wait, *watchers}, return_when=asyncio.FIRST_COMPLETED
        )
        for observer in watchers:
            if observer in done:
                await observer
    finally:
        for task in (ready_wait, stop_wait):
            task.cancel()
        await asyncio.gather(ready_wait, stop_wait, return_exceptions=True)


async def run_session(config_path, settings, session, display, *, worker_target=worker_process):
    context = multiprocessing.get_context("spawn")
    child_stop = context.Event()
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=worker_target, args=(config_path, child_stop, sender), name="ebki-worker"
    )
    tailer = LogTailer(settings.log_dir / "worker.jsonl")
    stopping = asyncio.Event()
    follow_stopped = asyncio.Event()
    ready = asyncio.Event()
    started = False
    tasks: list[asyncio.Task] = []
    failure: BaseException | None = None
    with worker.StopSignals() as signals:
        try:
            # 在子进程产生首条日志之前打开旧文件末尾，或记住新文件须从头读。
            for record in tailer.poll():
                console.render_log(display, record, settings.log_level)
            display.print("正在启动 worker…")
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
            )
            try:
                # 屏蔽期间尚未执行处理器的信号也代表取消，不能再启动业务进程。
                if not signals.is_set() and not signal.sigpending() & {
                    signal.SIGINT, signal.SIGTERM
                }:
                    process.start()
                    started = True
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            if signals.is_set():
                stopping.set()
                child_stop.set()
            sender.close()
            shutdown = asyncio.create_task(_watch_stop(signals, stopping, child_stop, display))
            tasks = [shutdown]
            if started:
                follower = asyncio.create_task(
                    _observe(
                        console._follow(tailer, display, settings.log_level, follow_stopped),
                        stopping,
                        display,
                    )
                )
                monitor = asyncio.create_task(
                    _observe(
                        _watch_worker(process, receiver, ready, stopping, signals), stopping, display
                    )
                )
                tasks.extend((follower, monitor))
                await _wait_ready(ready, stopping, (monitor, follower, shutdown))
                if ready.is_set() and not stopping.is_set():
                    await console.interact(
                        config_path,
                        session,
                        display,
                        stop_requested=stopping,
                        watchers=(monitor, follower, shutdown),
                    )
        except BaseException as exc:
            # 生命周期边界必须回收自己创建的进程；之后原样重新抛出，绝不伪造成功。
            failure = exc
        finally:
            stopping.set()
            child_stop.set()
            if started:
                while process.is_alive():
                    await asyncio.sleep(MONITOR_SECONDS)
                process.join()
            follow_stopped.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if failure is None:
                failure = next((item for item in results if isinstance(item, BaseException)), None)
            try:
                if started:
                    for record in tailer.drain():
                        console.render_log(display, record, settings.log_level)
            finally:
                tailer.close()
                receiver.close()
                sender.close()
                process.close()
        if failure is not None:
            raise failure
    display.print("console 和 worker 已退出。")


async def _run(config_path):
    settings = load_settings(config_path, command="run")
    session: PromptSession[str] = PromptSession(
        completer=WordCompleter(["help", *cli.CONSOLE_COMMANDS, "exit"]),
        history=InMemoryHistory(),
    )
    with StdoutProxy(raw=True) as output:
        display = Console(
            file=cast(TextIO, output), force_terminal=True, markup=False, highlight=False
        )
        await run_session(config_path, settings, session, display)


def run_interactive(config_path: str) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "run 需要交互终端；无人值守请使用 ebki worker，维护请使用单次命令。",
            file=sys.stderr,
        )
        return 2
    asyncio.run(_run(config_path))
    return 0
