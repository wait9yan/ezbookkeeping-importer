import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler


class StrictFileHandler(RotatingFileHandler):
    def handleError(self, record):
        sys.stderr.write("runtime log persistence failed\n")
        raise OSError("runtime log persistence failed")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Structured allowlist avoids accidental credentials, raw emails, or exception URLs.
        result = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in (
            "job_id",
            "source_row_id",
            "attempt_id",
            "target_id",
            "error_type",
            "duration_ms",
        ):
            if hasattr(record, key):
                result[key] = getattr(record, key)
        return json.dumps(result, ensure_ascii=False)


def configure_logging(settings):
    settings.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger = logging.getLogger("ebki")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        StrictFileHandler(
            settings.log_dir / "worker.jsonl",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backups,
            encoding="utf-8",
        ),
    ]
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger
