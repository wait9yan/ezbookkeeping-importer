"""统一 CLI 首次生成、严格已有文件及原子并发发布契约。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import time
import errno
import json
import os
from pathlib import Path
import signal
from threading import Barrier
from types import SimpleNamespace

import pytest

from ezbookkeeping_importer import config_initialization as initialization
from ezbookkeeping_importer.config import ConfigurationError, ENV_FIELDS, load_settings
from ezbookkeeping_importer.entrypoints import cli, run


ENVIRONMENT = {
    "EBKI_DATABASE_URL": "postgresql://synthetic@database.test/ebki",
    "EBKI_LEDGER_URL": "http://ledger.test",
    "EBKI_LEDGER_TOKEN": "synthetic-ledger-token",
    "EBKI_IMAP_USERNAME": "synthetic",
    "EBKI_IMAP_PASSWORD": "synthetic-password",
    "EBKI_AI_URL": "http://ai.test/v1",
    "EBKI_AI_MODEL": "synthetic-model",
    "EBKI_AI_TOKEN": "synthetic-ai-token",
}


@pytest.fixture
def command_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ENV_FIELDS.values():
        monkeypatch.delenv(key, raising=False)
    for key, value in ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
    loaded = []

    def runtime(settings, **kwargs):
        loaded.append(settings)
        return SimpleNamespace(
            store=SimpleNamespace(migrate=lambda: None, one=lambda sql: {"connected": True}),
            schema_version=2,
            ledger=SimpleNamespace(accounts=lambda: [], categories=lambda: []),
            close=lambda: None,
        )

    def run_service(path):
        loaded.append(load_settings(path, command="run"))
        return 0

    monkeypatch.setattr(cli, "Runtime", runtime)
    monkeypatch.setattr(run, "run_service", run_service)
    return loaded


@pytest.mark.parametrize("command", ["migrate", "doctor", "run"])
def test_first_cli_command_uses_published_business_defaults(command_environment, monkeypatch, command):
    monkeypatch.setattr(cli.sys, "argv", ["ebki", command])
    assert cli.main() == 0
    assert Path(cli.DEFAULT_CONFIG_PATH).read_bytes() == initialization.DEFAULT_CONFIG_RESOURCE.read_bytes()
    settings = command_environment[0]
    assert settings.timezone == "Asia/Shanghai"
    assert settings.date_only_time == time(12)
    assert settings.classification_mode == "ai"
    assert settings.mail.source_id == "qq-primary"
    assert settings.mail.rescan_days == 7
    assert settings.repayment_ownership_confirmed is False
    assert settings.repayments == settings.rules == ()
    assert settings.log_level == "INFO"
    assert not list(Path("data").glob(".config-*.tmp"))
    for value in ENVIRONMENT.values():
        assert value not in Path(cli.DEFAULT_CONFIG_PATH).read_text()


def test_repeated_cli_keeps_customized_bytes(command_environment, monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["ebki", "migrate"])
    assert cli.main() == 0
    path = Path(cli.DEFAULT_CONFIG_PATH)
    customized = path.read_bytes().replace(b'"qq-primary"', b'"custom-source"') + b"\n# retained\n"
    path.write_bytes(customized)
    assert cli.main() == 0
    assert path.read_bytes() == customized
    assert command_environment[-1].mail.source_id == "custom-source"


@pytest.mark.parametrize("content", [b"", b'password = "PRIVATE_VALUE'])
def test_existing_invalid_config_fails_without_replacing_or_echoing(
    command_environment, monkeypatch, capsys, content
):
    path = Path(cli.DEFAULT_CONFIG_PATH)
    path.parent.mkdir()
    path.write_bytes(content)
    monkeypatch.setattr(cli.sys, "argv", ["ebki", "migrate"])
    assert cli.main() == 1
    assert command_environment == []
    assert path.read_bytes() == content
    error = json.loads(capsys.readouterr().err)
    assert error["error_type"] == "ConfigurationError"
    assert "PRIVATE_VALUE" not in error["message"]


@pytest.mark.parametrize("arguments", [
    ["--config", "missing.toml", "migrate"],
    ["--config=data/config.toml", "migrate"],
    ["--conf=missing.toml", "migrate"],
])
def test_explicit_missing_file_never_initializes_default(command_environment, monkeypatch, capsys, arguments):
    monkeypatch.setattr(cli.sys, "argv", ["ebki", *arguments])
    assert cli.main() == 1
    assert command_environment == []
    assert not Path("data").exists()
    assert not Path("missing.toml").exists()
    assert json.loads(capsys.readouterr().err)["message"] == "business configuration file cannot be read"


@pytest.mark.parametrize("arguments,code", [
    (["--help"], 0), (["run", "--help"], 0), (["issues", "show", "--help"], 0),
    (["invalid-command"], 2),
])
def test_help_and_argument_errors_do_not_touch_data(command_environment, monkeypatch, arguments, code):
    # data 是一个普通文件，任何初始化尝试都会明确失败。
    Path("data").write_text("unchanged")
    monkeypatch.setattr(cli.sys, "argv", ["ebki", *arguments])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == code
    assert Path("data").read_text() == "unchanged"
    assert command_environment == []


def test_signals_installed_before_initialization(command_environment, monkeypatch):
    previous = signal.getsignal(signal.SIGTERM)

    def initialize(path):
        assert signal.getsignal(signal.SIGTERM) is not previous
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "initialize_default_config", initialize)
    monkeypatch.setattr(cli.sys, "argv", ["ebki", "migrate"])
    assert cli.main() == 128 + signal.SIGTERM
    assert command_environment == []
    assert signal.getsignal(signal.SIGTERM) is previous
    assert not Path("data").exists()


def test_existing_read_only_config_needs_no_directory_write(tmp_path):
    path = tmp_path / "config.toml"
    path.write_bytes(initialization.DEFAULT_CONFIG_RESOURCE.read_bytes())
    path.chmod(0o400)
    tmp_path.chmod(0o500)
    try:
        initialization.initialize_default_config(path)
        assert path.read_bytes() == initialization.DEFAULT_CONFIG_RESOURCE.read_bytes()
    finally:
        tmp_path.chmod(0o700)
        path.chmod(0o600)


@pytest.mark.parametrize("target_kind", ["file", "directory", "dangling_symlink", "symlink"])
def test_existing_target_is_never_replaced(tmp_path, target_kind):
    path = tmp_path / "config.toml"
    target = tmp_path / "other.toml"
    if target_kind == "file":
        path.write_bytes(b"invalid = [")
    elif target_kind == "directory":
        path.mkdir()
    else:
        if target_kind == "symlink":
            target.write_bytes(b"private-custom-value")
        path.symlink_to(target)
    inode = path.lstat().st_ino
    initialization.initialize_default_config(path)
    assert path.lstat().st_ino == inode
    if target_kind == "file":
        assert path.read_bytes() == b"invalid = ["
    elif target_kind == "dangling_symlink":
        assert not target.exists()
    elif target_kind == "symlink":
        assert target.read_bytes() == b"private-custom-value"


def test_concurrent_initialization_publishes_only_complete_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    content = initialization.DEFAULT_CONFIG_RESOURCE.read_bytes()
    participants = 8
    barrier = Barrier(participants)
    link = initialization.os.link

    def publish(source, target):
        assert Path(source).read_bytes() == content
        assert not path.exists()
        barrier.wait(timeout=5)
        try:
            link(source, target)
        finally:
            assert path.read_bytes() == content

    monkeypatch.setattr(initialization.os, "link", publish)
    with ThreadPoolExecutor(max_workers=participants) as pool:
        list(pool.map(initialization.initialize_default_config, [path] * participants))
    assert path.read_bytes() == content
    assert list(tmp_path.iterdir()) == [path]


def test_concurrent_external_creation_wins_without_overwrite(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    link = initialization.os.link

    def publish(source, target):
        path.write_bytes(b"customized-before-publication")
        link(source, target)

    monkeypatch.setattr(initialization.os, "link", publish)
    initialization.initialize_default_config(path)
    assert path.read_bytes() == b"customized-before-publication"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("boundary,code", [("fsync", errno.ENOSPC), ("link", errno.EACCES)])
def test_io_failures_are_explicit_and_clean_up(tmp_path, monkeypatch, boundary, code):
    path = tmp_path / "config.toml"

    def fail(*args):
        raise OSError(code, "PRIVATE_PATH_OR_VALUE")

    monkeypatch.setattr(initialization.os, boundary, fail)
    with pytest.raises(ConfigurationError) as error:
        initialization.initialize_default_config(path)
    assert errno.errorcode[code] in str(error.value)
    assert "PRIVATE_PATH_OR_VALUE" not in str(error.value)
    assert not list(tmp_path.iterdir())


def test_unwritable_directory_fails_cli_without_runtime(command_environment, monkeypatch, capsys):
    data = Path("data")
    data.mkdir(mode=0o500)
    monkeypatch.setattr(cli.sys, "argv", ["ebki", "migrate"])
    try:
        assert cli.main() == 1
        assert command_environment == []
        error = json.loads(capsys.readouterr().err)
        assert error["error_type"] == "ConfigurationError"
        assert "EACCES" in error["message"]
        assert not list(data.iterdir())
    finally:
        data.chmod(0o700)


def test_configuration_loader_stays_read_only(tmp_path, monkeypatch):
    for key in ENV_FIELDS.values():
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "missing.toml"
    with pytest.raises(ConfigurationError, match="cannot be read"):
        load_settings(str(path), command="migrate")
    assert not path.exists()
