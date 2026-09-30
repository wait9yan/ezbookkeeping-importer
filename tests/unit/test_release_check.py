"""正式版本发布不能把鉴权或网络错误当成不存在。"""
import importlib.util
import io
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest


spec = importlib.util.spec_from_file_location(
    "release_check", Path(__file__).parents[2] / "scripts" / "check-release.py"
)
assert spec is not None and spec.loader is not None
release_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_check)


@pytest.mark.parametrize("tag", ["v1.2", "1.2.3", "v1.2.3-rc1", "v9.9.9"])
def test_release_rejects_invalid_or_mismatched_tag(tmp_path, monkeypatch, tag):
    monkeypatch.chdir(tmp_path)
    Path("pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    with pytest.raises(ValueError):
        release_check.release_version(tag)


def test_release_accepts_matching_tag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    assert release_check.release_version("v1.2.3") == "1.2.3"


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTOR", "test-publisher")
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")


def test_registry_accepts_only_explicit_not_found(credentials):
    responses = [io.BytesIO(b'{"token":"synthetic"}'),
                 urllib.error.HTTPError("https://ghcr.io", 404, "not found", {}, None)]
    with patch.object(release_check.urllib.request, "urlopen", side_effect=responses):
        release_check.assert_absent("ghcr.io/test/repo", "1.2.3")


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_registry_propagates_auth_and_server_errors(credentials, code):
    responses = [io.BytesIO(b'{"token":"synthetic"}'),
                 urllib.error.HTTPError("https://ghcr.io", code, "failed", {}, None)]
    with patch.object(release_check.urllib.request, "urlopen", side_effect=responses):
        with pytest.raises(urllib.error.HTTPError):
            release_check.assert_absent("ghcr.io/test/repo", "1.2.3")


def test_registry_propagates_token_network_error(credentials):
    with patch.object(release_check.urllib.request, "urlopen",
                      side_effect=urllib.error.URLError("offline")):
        with pytest.raises(urllib.error.URLError):
            release_check.assert_absent("ghcr.io/test/repo", "1.2.3")


def test_registry_refuses_existing_version(credentials):
    with patch.object(release_check.urllib.request, "urlopen", side_effect=[
        io.BytesIO(b'{"token":"synthetic"}'), io.BytesIO(b'')
    ]):
        with pytest.raises(ValueError, match="拒绝覆盖"):
            release_check.assert_absent("ghcr.io/test/repo", "1.2.3")
