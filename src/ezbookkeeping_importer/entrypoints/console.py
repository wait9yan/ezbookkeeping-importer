"""统一运行内部的交互组件：复用既有维护命令。"""

import argparse
import asyncio
from collections.abc import Callable
import logging
import shlex

from rich.console import Console
from rich.text import Text

from ..application.events import EVENT_LABELS, format_event
from ..domain.errors import ImporterError
from . import cli
from .log_tail import LogNotice, LogTailer
from .presentation import help_text, render_help, render_result

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
    def __init__(self, command=None):
        self.command = command
        super().__init__(help_text(command))


class ConsoleArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CommandInputError(message)

    def print_help(self, file=None):
        command = self.prog.split()[-1]
        raise CommandHelp(command if command in cli.CONSOLE_COMMANDS else None)


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
            raise CommandHelp()
        if len(words) != 2 or words[1] not in (*cli.CONSOLE_COMMANDS, "exit"):
            raise CommandInputError("用法：help [" + "|".join((*cli.CONSOLE_COMMANDS, "exit")) + "]")
        raise CommandHelp(words[1])
    if words[0] == "exit":
        if len(words) != 1:
            raise CommandInputError("用法：exit")
        return argparse.Namespace(command="exit")
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


async def _execute(args, console: Console, execute: Callable):
    try:
        # Runtime 的创建、使用和关闭全部发生在这一次调用的线程内。
        result = await asyncio.to_thread(execute, args)
    except Exception as exc:
        error = cli.command_error(exc)
        console.print(Text(f"命令失败（{error['error_type']}）：{error['message']}", style="red"))
    else:
        render_result(console, args.command, result, args)


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
        return await session.prompt_async("ebki> ", handle_sigint=False)
    except (KeyboardInterrupt, EOFError):
        return "exit"


async def _next_line(session, watchers, stopping: asyncio.Event, active=None):
    prompt = asyncio.create_task(_prompt(session))
    stopped = asyncio.create_task(stopping.wait())
    try:
        watched = {prompt, stopped, *watchers}
        if active is not None:
            watched.add(active)
        while True:
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            for observer in watchers:
                if observer in done:
                    await observer
                    if not stopping.is_set():
                        raise ImporterError("运行监视已意外停止")
            if active in done:
                await active
                watched.remove(active)
            if stopped in done:
                return "exit"
            if prompt in done:
                return await prompt
    finally:
        for task in (prompt, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(prompt, stopped, return_exceptions=True)


async def interact(
    config_path: str,
    session,
    console: Console,
    *,
    stop_requested: asyncio.Event,
    watchers=(),
    execute: Callable = cli.execute_command,
):
    active: asyncio.Task | None = None
    console.print("worker 已就绪。输入 help 查看命令；exit、Ctrl+C 或 EOF 一起退出。")
    try:
        while not stop_requested.is_set():
            line = await _next_line(session, watchers, stop_requested, active)
            try:
                args = parse_line(line, config_path)
            except CommandHelp as exc:
                render_help(console, exc.command)
                continue
            except CommandInputError as exc:
                console.print(Text(f"命令未接受：{exc}", style="yellow"))
                continue
            if args is None:
                continue
            if args.command == "exit":
                break
            if active is not None and not active.done():
                console.print("上一条命令仍在执行，请完成后再试；help 和 exit 仍可使用。")
                continue
            if active is not None:
                await active
            console.print(f"正在执行 {args.command}…")
            active = asyncio.create_task(_execute(args, console, execute))
    finally:
        # 先请求 worker 停止，再等待已接受命令；命令线程不能被取消为假成功。
        stop_requested.set()
        if active is not None:
            if not active.done():
                console.print("正在等待已接受的命令完成，日志继续显示…")
            await active
