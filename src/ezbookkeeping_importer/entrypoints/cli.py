import argparse
import json
import re
import sys
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from datetime import date

from pydantic import ValidationError

from ..application import maintenance, issue_snapshot
from ..application.collect import request_sync, validate_scan_range
from ..application.recheck import request_recheck, request_snapshot_recheck
from ..bootstrap import Runtime
from ..config import load_settings
from ..config_initialization import initialize_default_config
from ..domain.errors import ImporterError, LogPersistenceError

DEFAULT_CONFIG_PATH = "data/config.toml"

def output(value):
    print(json.dumps(issue_snapshot.normalize_json(value), ensure_ascii=False, indent=2))


class CommandInterrupted(BaseException):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def maintenance_signals():
    def interrupt(signum, frame):
        raise CommandInterrupted(signum)

    previous = {sig: signal.signal(sig, interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def calendar_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期不存在") from exc


def _format(parser, *, inherited=False):
    parser.add_argument("--format", choices=("json", "text"),
                        default=argparse.SUPPRESS if inherited else "json")


def _filters(parser, *, required=False):
    parser.add_argument("--entity-type", required=required)
    parser.add_argument("--entity-id", required=required)
    parser.add_argument("--code")


def build_parser():
    parser = argparse.ArgumentParser(prog="ebki", description="银行邮件导入与单次维护命令")
    parser.add_argument("--config", default=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "migrate", "doctor", "restore-audit"):
        _format(commands.add_parser(name))
    commands.add_parser("run", help="无交互持续运行；通过进程信号停止")
    recheck = commands.add_parser("recheck", help="无参数时安排全库当前符合条件的重复候选复查")
    recheck.add_argument("--snapshot", help="仅复查快照内对象；- 从标准输入读取")
    _format(recheck)
    sync = commands.add_parser("sync")
    sync.add_argument("--since", type=calendar_date, help="补扫邮件接收日期下界（含，YYYY-MM-DD）")
    sync.add_argument("--until", type=calendar_date, help="补扫邮件接收日期上界（含，YYYY-MM-DD）")
    _format(sync)
    issues = commands.add_parser("issues", help="列出问题或查看、处理快照中的对象")
    _filters(issues)
    issues.add_argument("--status")
    issues.add_argument("--snapshot-out", help="将所列问题的完整前置快照写入文件")
    _format(issues)
    operations = issues.add_subparsers(dest="operation")
    show = operations.add_parser("show", help="输出完整详情、动作与前置快照")
    _filters(show, required=True)
    _format(show, inherited=True)
    candidates = operations.add_parser("candidates", help="查询一个快照项的候选账单及账户")
    candidates.add_argument("--snapshot", required=True)
    candidates.add_argument("--target-id")
    _format(candidates, inherited=True)
    resolution = operations.add_parser("resolve", help="依据一个快照项保存人工决定")
    resolution.add_argument("--snapshot", required=True)
    resolution.add_argument("--action", required=True,
                            choices=("retry", "ignore", "accept-source", "link", "confirm-new"))
    resolution.add_argument("--reason", required=True)
    resolution.add_argument("--target-id")
    resolution.add_argument("--account-id")
    _format(resolution, inherited=True)
    return parser


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("快照 JSON 不允许重复键")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("快照 JSON 不允许非有限数值")


def parse_command(parser, arguments=None):
    args = parser.parse_args(arguments)
    args.config_explicit = hasattr(args, "config")
    args.config = getattr(args, "config", DEFAULT_CONFIG_PATH)
    try:
        if args.command == "sync":
            validate_scan_range(args.since, args.until)
        operation = getattr(args, "operation", None)
        for key in ("entity_type", "entity_id", "code", "status", "target_id", "account_id", "snapshot_out"):
            value = getattr(args, key, None)
            if value is not None and not value.strip():
                parser.error(f"--{key.replace('_', '-')} 不允许为空")
        if operation is not None and (getattr(args, "status", None) or getattr(args, "snapshot_out", None)):
            parser.error("--status 和 --snapshot-out 仅用于 issues 列表")
        if operation in {"candidates", "resolve"} and any(
            getattr(args, key, None) for key in ("entity_type", "entity_id", "code")
        ):
            parser.error("候选查询与处理的对象身份仅由快照指定")
        if operation == "resolve":
            if not args.reason.strip():
                parser.error("--reason 必须是非空处理理由")
            if bool(args.target_id) != (args.action == "link"):
                parser.error("link 必须指定 --target-id；其他动作禁止 --target-id")
            if args.account_id is not None and args.action != "retry":
                parser.error("--account-id 仅用于 retry 修正交易账户")
        if getattr(args, "snapshot", None) is not None:
            content = sys.stdin.read() if args.snapshot == "-" else Path(args.snapshot).read_text(encoding="utf-8")
            document = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
            args.snapshot_document = issue_snapshot.validate_snapshot(
                document, single=operation in {"candidates", "resolve"}
            )
            if getattr(args, "account_id", None) is not None:
                if args.snapshot_document["items"][0]["issue"]["entity_type"] != "bank_transactions":
                    parser.error("--account-id 仅用于交易对象")
    except (ImporterError, ValueError, OSError) as exc:
        parser.error(str(exc) if isinstance(exc, ImporterError) else "快照文件不可读取或 JSON 格式非法")
    return args


def execute_command(args):
    """每次调用独立构建并关闭依赖，无交互命令共享应用用例。"""
    dependency_options: dict[str, Any] = {
        "command": "resolve"
        if getattr(args, "operation", None) in {"candidates", "resolve"}
        else args.command,
        "action": "link"
        if getattr(args, "operation", None) == "candidates"
        else getattr(args, "action", None),
        "account_id": getattr(args, "account_id", None),
    }
    settings = load_settings(args.config, **dependency_options)
    runtime = Runtime(settings, **dependency_options)
    try:
        return _execute(args, runtime)
    finally:
        runtime.close()


def _execute(args, runtime):
    store = runtime.store
    if args.command == "migrate":
        return {"schema_version": runtime.schema_version}
    if args.command == "sync":
        return {
            "queued": request_sync(store, args.since, args.until),
            "since": args.since,
            "until": args.until,
        }
    if args.command == "status":
        return maintenance.status(store)
    if args.command == "recheck":
        if getattr(args, "snapshot_document", None) is not None:
            return request_snapshot_recheck(store, args.snapshot_document)
        return request_recheck(store)
    if args.command == "issues":
        operation = getattr(args, "operation", None)
        if operation == "candidates":
            return issue_snapshot.snapshot_candidates(
                store, runtime.ledger, args.snapshot_document, args.target_id
            )
        if operation == "resolve":
            return issue_snapshot.resolve_snapshot(
                store, runtime.optional_ledger, args.snapshot_document, args.action,
                args.reason, args.target_id, args.account_id,
            )
        if operation == "show" or getattr(args, "snapshot_out", None):
            snapshot = issue_snapshot.snapshot_issues(
                store, args.entity_type, args.entity_id, args.code, getattr(args, "status", None)
            )
            if operation == "show":
                return snapshot
            Path(args.snapshot_out).write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            return [item["issue"] for item in snapshot["items"]]
        items = maintenance.issues(store, args.entity_type, args.entity_id)
        return [item for item in items
                if (args.code is None or item["code"] == args.code)
                and (args.status is None or item.get("status") == args.status)]
    if args.command == "doctor":
        return {
            "database": bool(store.one("SELECT 1 AS connected")),
            "schema_ready": True,
            "schema_version": runtime.schema_version,
            "account_count": len(runtime.ledger.accounts()),
            "category_count": len(runtime.ledger.categories()),
            "configuration_valid": True,
            "imap_connection": "not_checked",
            "ai_connection": "not_checked",
            "note": "配置已校验，检查数据库结构就绪和账本只读连通性；未连接 IMAP/AI，未执行生产写入验收",
        }
    if args.command == "restore-audit":
        return maintenance.restore_audit(store, runtime.ledger)
    raise ImporterError("unknown command")


def command_error(exc: Exception) -> dict:
    # Never echo DSNs, API tokens, original messages or HTTP request objects.
    message = (
        str(exc) if isinstance(exc, ImporterError) else "命令失败；检查配置或 issues 中的业务原因"
    )
    error: dict = {"error_type": type(exc).__name__, "message": message}
    if isinstance(exc, LogPersistenceError):
        error["message"] = "日志持久化失败；已提交业务保持原状态，请检查日志目录与磁盘"
    if isinstance(exc, ValidationError):
        error["fields"] = [
            {
                "path": ".".join(map(str, item["loc"])),
                "type": item["type"],
                "message": "invalid configuration value",
            }
            for item in exc.errors(include_input=False, include_context=False)
        ]
    return error


def main():
    try:
        with maintenance_signals():
            args = parse_command(build_parser())
            if not args.config_explicit:
                initialize_default_config(Path(args.config))
            if args.command != "run":
                result = execute_command(args)
                if args.format == "json":
                    output(result)
                else:
                    from rich.console import Console
                    from .presentation import render_result

                    render_result(Console(), args.command, result, args)
                return 0
        from .run import run_service

        return run_service(args.config)
    except CommandInterrupted as exc:
        print(json.dumps({"error_type": "Interrupted", "message":
                          "维护命令已中断；部分操作可能已提交，请重新查询状态，不会自动重放。"},
                         ensure_ascii=False), file=sys.stderr)
        return 128 + exc.signum
    except Exception as exc:
        print(json.dumps(command_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1
