"""容器冒烟配置必须经过应用真实边界校验。"""
import importlib.util
import os
from pathlib import Path

import pytest

from ezbookkeeping_importer.config import load_settings


spec = importlib.util.spec_from_file_location(
    "verify_image", Path(__file__).parents[2] / "scripts" / "verify-image.py"
)
assert spec is not None and spec.loader is not None
verify_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_image)


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
