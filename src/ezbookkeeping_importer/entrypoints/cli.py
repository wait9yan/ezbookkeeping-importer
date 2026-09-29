import argparse
import json
import re
import sys
from typing import Any
from datetime import date

from pydantic import ValidationError

from ..application import maintenance, issue_interaction
from ..application.collect import request_sync, validate_scan_range
from ..application.resolve import resolve
from ..application.recheck import request_recheck
from ..bootstrap import Runtime
from ..config import load_settings
from ..domain.errors import ImporterError, LogPersistenceError
from .worker import run

CONSOLE_COMMANDS = ("status", "issues", "sync", "recheck")


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def calendar_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期不存在") from exc


def build_parser(*, interactive=False, parser_class=argparse.ArgumentParser):
    parser = parser_class(prog="ebki", description="银行邮件导入维护命令")
    if not interactive:
        parser.add_argument("--config", default="config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("recheck", help="安排一次重复候选复查；通过正常检查后继续入账")
    if not interactive:
        for name in ("migrate", "doctor", "restore-audit", "run"):
            commands.add_parser(name)
        worker = commands.add_parser("worker")
        worker.add_argument("--once", action="store_true")
    sync = commands.add_parser("sync")
    sync.add_argument("--since", type=calendar_date, help="补扫邮件接收日期下界（含，YYYY-MM-DD）")
    sync.add_argument("--until", type=calendar_date, help="补扫邮件接收日期上界（含，YYYY-MM-DD）")
    issues = commands.add_parser("issues")
    issues.add_argument("--entity-type")
    issues.add_argument("--entity-id")
    return parser


def parse_command(parser, arguments=None):
    args = parser.parse_args(arguments)
    if args.command == "sync":
        try:
            validate_scan_range(args.since, args.until)
        except ImporterError as exc:
            parser.error(str(exc))
    return args


def execute_command(args):
    """每次调用独立构建并关闭依赖，CLI 和控制台共享同一用例分发。"""
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
        store.migrate()
        return {"schema_version": 1}
    if args.command == "worker":
        run(runtime, args.once)
        return None
    if args.command == "sync":
        return {
            "queued": request_sync(store, args.since, args.until),
            "since": args.since,
            "until": args.until,
        }
    if args.command == "status":
        return maintenance.status(store)
    if args.command == "recheck":
        return request_recheck(store, getattr(args, "targets", None))
    if args.command == "issues":
        if getattr(args, "operation", None) == "detail":
            return issue_interaction.issue_detail(store, args.selected)
        if getattr(args, "operation", None) == "candidates":
            return issue_interaction.issue_candidates(
                store, runtime.ledger, args.selected, getattr(args, "target_id", None)
            )
        if getattr(args, "operation", None) != "resolve":
            return maintenance.issues(store, args.entity_type, args.entity_id)
    if args.command == "issues" and getattr(args, "operation", None) == "resolve":
        return resolve(
            store,
            runtime.optional_ledger,
            args.entity_type,
            args.entity_id,
            args.version,
            args.action,
            args.reason,
            args.target_id,
            args.account_id,
            args.code,
            getattr(args, "selected", None),
        )
    if args.command == "doctor":
        return {
            "database": bool(store.one("SELECT 1 AS connected")),
            "account_count": len(runtime.ledger.accounts()),
            "category_count": len(runtime.ledger.categories()),
            "configuration_valid": True,
            "imap_connection": "not_checked",
            "ai_connection": "not_checked",
            "note": "配置已校验，仅检查数据库与账本只读连通性；未连接 IMAP/AI，未执行生产写入验收",
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
    args = parse_command(build_parser())
    try:
        if args.command == "run":
            from .run import run_interactive

            return run_interactive(args.config)
        result = execute_command(args)
        if args.command != "worker":
            output(result)
    except Exception as exc:
        print(json.dumps(command_error(exc), ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
