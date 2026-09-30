"""容器冒烟配置必须经过应用真实边界校验。"""
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from ezbookkeeping_importer.config import load_settings
from ezbookkeeping_importer.config_initialization import initialize_default_config


spec = importlib.util.spec_from_file_location(
    "verify_image", Path(__file__).parents[2] / "scripts" / "verify-image.py"
)
assert spec is not None and spec.loader is not None
verify_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_image)


@pytest.mark.parametrize("postgres_args,expected_image", [
    ([], "postgres:17-bookworm"),
    (["--postgres-image", "postgres:18-alpine"], "postgres:18-alpine"),
])
def test_cli_routes_postgres_image_to_database_container(monkeypatch, postgres_args, expected_image):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["docker", "run", "-d"]:
            raise subprocess.CalledProcessError(1, command, stderr="synthetic launch failure")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(verify_image.subprocess, "run", run)
    with pytest.raises(SystemExit, match="synthetic launch failure"):
        verify_image.main(["ebki-tested:arm64", "--platform", "linux/arm64", *postgres_args])

    assert commands[0] == [
        "docker", "run", "--rm", "--platform", "linux/arm64", "--network", "none",
        "ebki-tested:arm64", "--help",
    ]
    database_command = next(command for command in commands if command[:3] == ["docker", "run", "-d"])
    assert database_command[-1] == expected_image
    assert any(command[:3] == ["docker", "volume", "rm"] for command in commands)
    assert any(command[:3] == ["docker", "network", "rm"] for command in commands)


def test_initial_image_configuration_uses_real_packaged_defaults(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("EBKI_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("EBKI_DATABASE_URL", "postgresql://synthetic@database.test/ebki")
    path = tmp_path / "config.toml"
    initialize_default_config(path)
    settings = load_settings(str(path), command="migrate")
    assert settings.classification_mode == "ai"
    assert settings.mail.source_id == "qq-primary"
    exec(verify_image.DEFAULT_CONFIG_CHECK.replace("Path('/app/data/config.toml')", f"Path({str(path)!r})"))


@pytest.mark.parametrize("command", ["migrate", "status", "run"])
def test_smoke_config_passes_real_configuration_boundary(tmp_path, monkeypatch, command):
    for key in os.environ:
        if key.startswith("EBKI_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("EBKI_DATABASE_URL", "postgresql://synthetic@database.test/ebki")
    if command == "run":
        from image_lifecycle import ENVIRONMENT

        for key, value in ENVIRONMENT.items():
            monkeypatch.setenv(key, value)
    config = tmp_path / "smoke.toml"
    config.write_text(verify_image.SMOKE_CONFIG)
    settings = load_settings(str(config), command=command)
    assert settings.classification_mode == "rules_only"
    assert settings.timezone == "Asia/Shanghai"
    assert settings.mail.source_id == "isolated-smoke"
