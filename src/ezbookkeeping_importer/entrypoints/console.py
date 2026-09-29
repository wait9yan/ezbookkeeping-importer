"""独立交互终端：只读 worker 日志，复用既有维护命令。"""

import argparse
import asyncio
from collections.abc import Callable
import json
import logging
from pathlib import Path
import shlex
import sys
from typing import TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import StdoutProxy
from rich.console import Console
from rich.text import Text

from ..application.events import EVENT_LABELS, format_event
from ..config import load_settings
from ..domain.errors import ImporterError
from . import cli
from .log_tail import LogNotice, LogTailer

POLL_SECONDS = 0.25
LEVEL_STYLES = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}


class CommandInputError(ImporterError):
    pass


class CommandHelp(Exception):
    pass


class ConsoleArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CommandInputError(message)

    def print_help(self, file=None):
        raise CommandHelp(self.format_help())


def parse_line(line: str, config_path: str):
    try:
        words = shlex.split(line)
    except ValueError:
        raise CommandInputError("命令引号不完整，请检查后重试") from None
    if not words:
        return None
    parser = cli.build_parser(interactive=True, parser_class=ConsoleArgumentParser)
    if words[0] == "help":
        if len(words) == 1:
            raise CommandHelp(parser.format_help() + "\nhelp [命令] 查看帮助；quit 退出控制台。")
        if len(words) != 2 or words[1] not in cli.CONSOLE_COMMANDS:
            raise CommandInputError("用法：help [status|issues|sync|resolve]")
        words = [words[1], "--help"]
    if words[0] == "quit":
        if len(words) != 1:
            raise CommandInputError("用法：quit")
        return argparse.Namespace(command="quit")
    args = cli.parse_command(parser, words)
    args.config = config_path
    return args


def render_log(console: Console, record: dict | LogNotice, minimum_level: str):
    if isinstance(record, LogNotice):
        console.print(Text(record.message, style=LEVEL_STYLES.get(record.level, "")))
        return
    level = record.get("level", "INFO")
    if not isinstance(level, str) or level not in LEVEL_STYLES:
        console.print(Text("日志级别无法识别，以下事件按 INFO 显示", style="yellow"))
        level = "INFO"
    if logging.getLevelName(level) < logging.getLevelName(minimum_level):
        return
    text = Text()
    timestamp = record.get("time")
    if isinstance(timestamp, str):
        text.append("".join(c for c in timestamp[:32] if c.isprintable()) + " ", style="dim")
    text.append(f"{level:<8} ", style=LEVEL_STYLES[level])
    if record["event"] not in EVENT_LABELS:
        text.append("未识别事件：", style="yellow")
    text.append(format_event(record))
    console.print(text)


def render_result(console: Console, command: str, result):
    if command == "sync":
        console.print(
            "同步请求已排队。" if result["queued"] else "同步请求已合并到现有待处理任务。"
        )
    elif command == "resolve":
        if result.get("result") == "intent recorded; external outcome must be verified":
            console.print("处理意图已保存；现有写入结果仍待核实。")
        else:
            console.print("处理决定已保存；后台执行结果请查看后续事件或 issues。")
    console.print_json(json.dumps(result, ensure_ascii=False, default=str))


async def _execute(args, console: Console, execute: Callable):
    try:
        # Runtime 的创建、使用和关闭全部发生在这一次调用的线程内。
        result = await asyncio.to_thread(execute, args)
    except Exception as exc:
        error = cli.command_error(exc)
        console.print(Text(f"命令失败（{error['error_type']}）：{error['message']}", style="red"))
    else:
        render_result(console, args.command, result)


async def _follow(tailer: LogTailer, console: Console, minimum_level: str, stopped: asyncio.Event):
    while not stopped.is_set():
        try:
            for record in await asyncio.to_thread(tailer.poll):
                render_log(console, record, minimum_level)
        except Exception as exc:
            raise ImporterError(f"日志跟随已中断（{type(exc).__name__}）") from exc
        try:
            await asyncio.wait_for(stopped.wait(), POLL_SECONDS)
        except TimeoutError:
            continue


async def _prompt(session):
    try:
        return await session.prompt_async("ebki> ")
    except KeyboardInterrupt:
        return ""
    except EOFError:
        return "quit"


async def _next_line(session, follower: asyncio.Task, active: asyncio.Task | None = None):
    prompt = asyncio.create_task(_prompt(session))
    try:
        watched = {prompt, follower}
        if active is not None:
            watched.add(active)
        while True:
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            if follower in done:
                await follower
                raise ImporterError("日志跟随已意外停止")
            if active in done:
                await active
                watched.remove(active)
            if prompt in done:
                return await prompt
    finally:
        if not prompt.done():
            prompt.cancel()
            await asyncio.gather(prompt, return_exceptions=True)


async def interact(
    config_path: str,
    log_path: Path,
    minimum_level: str,
    session,
    console: Console,
    *,
    execute: Callable = cli.execute_command,
):
    tailer = LogTailer(log_path)
    stopped = asyncio.Event()
    follower = asyncio.create_task(_follow(tailer, console, minimum_level, stopped))
    active: asyncio.Task | None = None
    console.print("控制台已启动。输入 help 查看命令；Ctrl+C 清除输入；quit 退出控制台。")
    console.print("显示启动后的新日志。退出时等待已接受的命令完成；退出不会停止 worker。")
    try:
        while True:
            line = await _next_line(session, follower, active)
            try:
                args = parse_line(line, config_path)
            except CommandHelp as exc:
                console.print(Text(str(exc)))
                continue
            except CommandInputError as exc:
                console.print(Text(f"命令未接受：{exc}", style="yellow"))
                continue
            if args is None:
                continue
            if args.command == "quit":
                break
            if active is not None and not active.done():
                console.print("上一条命令仍在执行，请完成后再试；help 和 quit 仍可使用。")
                continue
            if active is not None:
                await active
            console.print(f"正在执行 {args.command}…")
            active = asyncio.create_task(_execute(args, console, execute))
    finally:
        try:
            if active is not None:
                if not active.done():
                    console.print("正在等待已接受的命令完成，日志继续显示…")
                await active
        finally:
            stopped.set()
            try:
                await follower
            finally:
                tailer.close()
    console.print("控制台已退出。")


async def _run(config_path: str):
    settings = load_settings(config_path, command="console")
    session: PromptSession[str] = PromptSession(
        completer=WordCompleter(["help", *cli.CONSOLE_COMMANDS, "quit"]),
        history=InMemoryHistory(),
    )
    # 代理在当前 event loop/AppSession 中构造；Rich 的所有输出都从这里进入。
    with StdoutProxy(raw=True) as output:
        console = Console(
            file=cast(TextIO, output), force_terminal=True, markup=False, highlight=False
        )
        await interact(
            config_path, settings.log_dir / "worker.jsonl", settings.log_level, session, console
        )


def run_console(config_path: str) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "console 需要交互终端；请在终端运行，或使用 ebki status/issues/sync/resolve 单次命令。",
            file=sys.stderr,
        )
        return 2
    asyncio.run(_run(config_path))
    return 0
