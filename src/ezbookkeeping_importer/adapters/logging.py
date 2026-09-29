import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from ..application.events import safe_event, format_event
from ..domain.errors import LogPersistenceError


class StrictFileHandler(RotatingFileHandler):
    def handleError(self, record):
        sys.stderr.write("runtime log persistence failed\n")
        raise LogPersistenceError("runtime log persistence failed")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = safe_event(
            {
                **record.__dict__,
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "event": record.getMessage(),
                **getattr(record, "event_data", {}),
            }
        )
        return json.dumps(result, ensure_ascii=False)


class TerminalHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        from rich.console import Console

        self.console = Console(file=sys.stdout, highlight=False)

    def emit(self, record):
        data = json.loads(JsonFormatter().format(record))
        self.console.print(
            f"{data['time']} {data['level']:<8} {format_event(data)}",
            markup=False,
            style="red"
            if record.levelno >= logging.ERROR
            else "yellow"
            if record.levelno >= logging.WARNING
            else None,
        )


def configure_logging(settings):
    settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger("ebki")
    logger.setLevel(settings.log_level)
    logger.propagate = False
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    handlers: list[logging.Handler] = [
        StrictFileHandler(
            settings.log_dir / "worker.jsonl",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backups,
            encoding="utf-8",
        ),
        TerminalHandler() if sys.stdout.isatty() else logging.StreamHandler(sys.stdout),
    ]
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
