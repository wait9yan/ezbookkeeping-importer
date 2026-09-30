"""单进程入口在依赖初始化前安装信号处理，并始终关闭资源。"""
import os
from pathlib import Path
import signal
import subprocess
import shutil
import sys
from unittest.mock import Mock

import pytest

from ezbookkeeping_importer.entrypoints import cli, run
from ezbookkeeping_importer.config_initialization import DEFAULT_CONFIG_RESOURCE


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("boundary", ["settings", "runtime", "worker"])
def test_stop_during_startup_and_processing_restores_handlers(monkeypatch, sig, boundary):
    previous = signal.getsignal(sig)
    runtime = Mock()

    def settings(*args, **kwargs):
        assert signal.getsignal(sig) != previous
        if boundary == "settings":
            os.kill(os.getpid(), sig)
        return object()

    def create(*args, **kwargs):
        if boundary == "runtime":
            os.kill(os.getpid(), sig)
        return runtime

    def process(actual, *, stop_event):
        assert actual is runtime
        if boundary == "worker":
            os.kill(os.getpid(), sig)
            os.kill(os.getpid(), sig)
        assert stop_event.is_set()

    monkeypatch.setattr(run, "load_settings", settings)
    constructor = Mock(side_effect=create)
    monkeypatch.setattr(run, "Runtime", constructor)
    process_mock = Mock(side_effect=process)
    monkeypatch.setattr(run.worker, "run", process_mock)
    assert run.run_service("config.toml") == 0
    assert signal.getsignal(sig) is previous
    if boundary == "settings":
        constructor.assert_not_called()
        process_mock.assert_not_called()
    else:
        runtime.close.assert_called_once()


def test_runtime_failure_closes_resources_without_masking_error(monkeypatch):
    runtime = Mock()
    monkeypatch.setattr(run, "load_settings", Mock())
    monkeypatch.setattr(run, "Runtime", Mock(return_value=runtime))
    monkeypatch.setattr(run.worker, "run", Mock(side_effect=RuntimeError("failure")))
    with pytest.raises(RuntimeError, match="failure"):
        run.run_service("config.toml")
    runtime.close.assert_called_once()


def test_run_never_reads_stdin_or_requires_a_terminal(monkeypatch):
    import sys
    stdin = Mock()
    stdin.read.side_effect = AssertionError("read stdin")
    stdin.isatty.side_effect = AssertionError("checked TTY")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(run, "load_settings", Mock())
    monkeypatch.setattr(run, "Runtime", Mock())
    monkeypatch.setattr(run.worker, "run", Mock())
    assert run.run_service("config.toml") == 0
    stdin.read.assert_not_called()
    stdin.isatty.assert_not_called()


def test_launcher_uses_own_root_and_preserves_argument_boundaries(tmp_path):
    root = tmp_path / "project with spaces"
    root.mkdir()
    launcher = root / "run"
    launcher.write_bytes(Path("run").read_bytes())
    launcher.chmod(0o755)
    commands = tmp_path / "bin"
    commands.mkdir()
    uv = commands / "uv"
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
    uv.chmod(0o755)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"]}
    default = subprocess.run(
        [str(launcher)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5, check=True
    )
    assert default.stdout.splitlines() == [str(root), "run", "--env-file", ".env", "ebki", "run"]
    args = ["--config", "other config.toml", "resolve", "email", "one", "--reason", "中文 理由"]
    explicit = subprocess.run(
        [str(launcher), *args], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=5, check=True
    )
    assert explicit.stdout.splitlines()[5:] == args


def test_launcher_real_cli_creates_defaults_before_missing_environment_error(tmp_path):
    root = tmp_path / "isolated project"
    root.mkdir()
    launcher = root / "run"
    launcher.write_bytes(Path("run").read_bytes())
    launcher.chmod(0o755)
    (root / ".env").write_text("# isolated; no service credentials\n")
    assert shutil.which("uv") is not None
    environment = {key: value for key, value in os.environ.items() if not key.startswith("EBKI_")}
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment["PATH"]
    result = subprocess.run(
        [str(launcher), "migrate"], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 1
    assert '"error_type": "ConfigurationError"' in result.stderr
    assert "EBKI_DATABASE_URL" in result.stderr
    assert (root / cli.DEFAULT_CONFIG_PATH).read_bytes() == DEFAULT_CONFIG_RESOURCE.read_bytes()
    assert not (tmp_path / cli.DEFAULT_CONFIG_PATH).exists()


@pytest.mark.parametrize("arguments", [["worker"], ["worker", "--once"], ["run", "--once"]])
def test_removed_worker_entrypoints_fail_before_resources(arguments, monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["ebki", *arguments])
    monkeypatch.setattr(cli, "Runtime", lambda *a, **k: pytest.fail("created runtime"))
    monkeypatch.setattr(cli, "load_settings", lambda *a, **k: pytest.fail("loaded config"))
    monkeypatch.setattr(run, "run_service", lambda *a: pytest.fail("started owner"))
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


def test_cli_help_has_only_one_running_entrypoint():
    help_text = cli.build_parser().format_help()
    assert "run" in help_text
    assert "worker" not in help_text
    assert "--once" not in help_text
