import argparse
import json
import sys
import re
from datetime import date
from pathlib import Path

from ..bootstrap import Runtime
from ..config import load_settings
from ..application.collect import ingest, request_sync, validate_scan_range
from ..application.resolve import resolve
from .worker import run
from ..application import maintenance
from ..domain.errors import ImporterError
from pydantic import ValidationError


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def calendar_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("日期格式必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期不存在") from exc


def main():
    parser = argparse.ArgumentParser(description="银行邮件导入维护命令")
    parser.add_argument("--config", default="config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "status", "doctor", "restore-audit"):
        commands.add_parser(name)
    sync = commands.add_parser("sync")
    sync.add_argument("--since", type=calendar_date, help="补扫邮件接收日期下界（含，YYYY-MM-DD）")
    sync.add_argument("--until", type=calendar_date, help="补扫邮件接收日期上界（含，YYYY-MM-DD）")
    worker = commands.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    issues = commands.add_parser("issues")
    issues.add_argument("--id", type=int)
    issues.add_argument("--all", action="store_true")
    resolution = commands.add_parser("resolve")
    resolution.add_argument("id", type=int)
    resolution.add_argument("--version", required=True, type=int)
    resolution.add_argument(
        "--action",
        required=True,
        choices=["accept-source", "ignore", "link", "retry", "confirm-new"],
    )
    resolution.add_argument("--reason", required=True)
    resolution.add_argument("--target-id")
    resolution.add_argument("--account-id")
    importing = commands.add_parser("import-eml")
    importing.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if args.command == "sync":
        try:
            validate_scan_range(args.since, args.until)
        except ImporterError as exc:
            parser.error(str(exc))
    runtime = None
    try:
        dependency_options = {
            "command": args.command,
            "action": getattr(args, "action", None),
            "account_id": getattr(args, "account_id", None),
        }
        settings = load_settings(args.config, **dependency_options)
        runtime = Runtime(settings, **dependency_options)
        store = runtime.store
        if args.command == "migrate":
            store.migrate()
            output({"schema_version": 1})
        elif args.command == "worker":
            run(runtime, args.once)
        elif args.command == "sync":
            output(
                {
                    "queued": request_sync(store, args.since, args.until),
                    "since": args.since,
                    "until": args.until,
                }
            )
        elif args.command == "import-eml":
            output(
                {
                    "message_ids": [
                        ingest(store, runtime.evidence, Path(path).read_bytes(), settings, "eml")
                        for path in args.paths
                    ]
                }
            )
        elif args.command == "status":
            output(maintenance.status(store, settings))
        elif args.command == "issues":
            output(maintenance.issues(store, args.id, args.all))
        elif args.command == "resolve":
            output(
                resolve(
                    store,
                    runtime.optional_ledger,
                    args.id,
                    args.version,
                    args.action,
                    args.reason,
                    args.target_id,
                    args.account_id,
                )
            )
        elif args.command == "doctor":
            output(
                {
                    "database": bool(store.one("SELECT 1 AS connected")),
                    "account_count": len(runtime.ledger.accounts()),
                    "category_count": len(runtime.ledger.categories()),
                    "source_policy": settings.source_policy,
                    "writes_enabled": settings.writes_enabled,
                    "configuration_valid": True,
                    "imap_connection": "not_checked",
                    "ai_connection": "not_checked",
                    "note": "配置已校验，仅检查数据库与账本只读连通性；未连接 IMAP/AI，未执行生产写入验收",
                }
            )
        elif args.command == "restore-audit":
            output(maintenance.restore_audit(store, runtime.ledger))
    except Exception as exc:
        # Never echo DSNs, API tokens, original messages or HTTP request objects.
        message = (
            str(exc)
            if isinstance(exc, ImporterError)
            else "命令失败；检查配置或 issues 中的业务原因"
        )
        error: dict = {"error_type": type(exc).__name__, "message": message}
        if isinstance(exc, ValidationError):
            error["fields"] = [
                {
                    "path": ".".join(map(str, item["loc"])),
                    "type": item["type"],
                    "message": "invalid configuration value",
                }
                for item in exc.errors(include_input=False, include_context=False)
            ]
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if runtime:
            runtime.close()
    return 0
