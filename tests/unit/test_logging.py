"""日志持久化与失败可见性回归，不依赖数据库或外部服务。"""

import json
import logging
from types import SimpleNamespace

import pytest

from ezbookkeeping_importer.adapters.logging import configure_logging


def settings(path, size=10000):
    return SimpleNamespace(log_dir=path, log_max_bytes=size, log_backups=2, log_level="INFO")


def close_logger(logger):
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_file_console_and_restart_append(tmp_path, capsys):
    configuration = settings(tmp_path)
    logger = configure_logging(configuration)
    try:
        logger.info("first_event", extra={"task_id": 12, "password": "test-secret"})
        console = json.loads(capsys.readouterr().out)
        first = json.loads((tmp_path / "worker.jsonl").read_text())
        assert console["event"] == first["event"] == "first_event"
        assert console["task_id"] == first["task_id"] == 12
        assert "test-secret" not in json.dumps([console, first])
        logger = configure_logging(configuration)
        logger.info("after_restart")
        lines = (tmp_path / "worker.jsonl").read_text().splitlines()
        assert [json.loads(line)["event"] for line in lines] == ["first_event", "after_restart"]
    finally:
        close_logger(logger)


def test_rotation_leaves_other_evidence_untouched(tmp_path, capsys):
    evidence = tmp_path / "evidence.eml"
    evidence.write_bytes(b"preserve original evidence")
    logger = configure_logging(settings(tmp_path, size=250))
    try:
        for number in range(20):
            logger.info("event_" + str(number), extra={"task_id": number})
        assert (tmp_path / "worker.jsonl.1").is_file()
        assert (tmp_path / "worker.jsonl.2").is_file()
        assert not (tmp_path / "worker.jsonl.3").exists()
        assert evidence.read_bytes() == b"preserve original evidence"
        for path in tmp_path.glob("worker.jsonl*"):
            for line in path.read_text().splitlines():
                assert json.loads(line)["level"] == "INFO"
        assert len(capsys.readouterr().out.splitlines()) == 20
    finally:
        close_logger(logger)


def test_runtime_disk_error_is_explicit(tmp_path, monkeypatch, capsys):
    logger = configure_logging(settings(tmp_path))
    try:
        handler = next(h for h in logger.handlers if isinstance(h, logging.FileHandler))

        def fail_write(value):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(handler.stream, "write", fail_write)
        with pytest.raises(OSError, match="runtime log persistence failed"):
            logger.info("must_persist")
        assert "runtime log persistence failed" in capsys.readouterr().err
    finally:
        close_logger(logger)


def test_invalid_log_directory_fails(tmp_path):
    path = tmp_path / "not-a-directory"
    path.write_text("occupied")
    with pytest.raises(FileExistsError):
        configure_logging(settings(path))


def test_tty_worker_keeps_timestamp_and_level_without_losing_chinese_title(tmp_path, monkeypatch, capsys):
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    logger = configure_logging(settings(tmp_path))
    try:
        logger.warning("worker_stop_requested", extra={"reason": "SIGTERM"})
        rendered = capsys.readouterr().out
        stored = json.loads((tmp_path / "worker.jsonl").read_text())
        assert stored["time"][:10] in rendered
        assert "WARNING" in rendered and "已收到停止请求" in rendered
    finally:
        close_logger(logger)
