#!/usr/bin/env python3
"""使用隔离网络、数据库和数据卷验证待发布镜像，不读取本地 .env。"""
import argparse
import json
import secrets
import subprocess
import time
import uuid
import sys
import tempfile
from pathlib import Path

# Support importlib-based unit tests as well as direct execution.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_lifecycle import verify_lifecycle  # noqa: E402


SMOKE_CONFIG = '''timezone = "Asia/Shanghai"
classification_mode = "rules_only"
[mail]
source_id = "isolated-smoke"
'''

DEFAULT_CONFIG_CHECK = '''
from importlib.resources import files
from pathlib import Path
import tomllib
from ezbookkeeping_importer.config import BusinessSettings
path = Path('/app/data/config.toml')
assert path.read_bytes() == files('ezbookkeeping_importer').joinpath('config.toml').read_bytes()
settings = BusinessSettings.model_validate(tomllib.loads(path.read_text()))
assert settings.classification_mode == 'ai'
assert settings.mail.source_id == 'qq-primary'
assert settings.timezone == 'Asia/Shanghai'
'''


def expect_configuration_error(*args: str, message: str) -> None:
    try:
        docker(*args, timeout=60)
    except subprocess.CalledProcessError as exc:
        assert exc.returncode == 1, exc.returncode
        error = json.loads(exc.stderr)
        assert error['error_type'] == 'ConfigurationError', error['error_type']
        assert message in error['message'], error['message']
        return
    raise AssertionError('配置错误路径意外成功')


def docker(*args: str, timeout: int = 120) -> str:
    return subprocess.run(
        ["docker", *args], check=True, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout,
    ).stdout.strip()


def verify(image: str, platform: str | None) -> None:
    name = f"ebki-smoke-{uuid.uuid4().hex[:12]}"
    network, database = f"{name}-net", f"{name}-db"
    password = secrets.token_urlsafe(24)
    volume = f"{name}-data"
    volume_created = False
    network_created = False
    database_created = False
    platform_args = ["--platform", platform] if platform else []
    mounts = ["--mount", f"type=volume,src={volume},dst=/app/data"]
    base = ["run", "--rm", *platform_args]
    try:
        print(docker(*base, "--network", "none", image, "--help"))
        docker("network", "create", "--internal", network)
        network_created = True
        docker("volume", "create", volume)
        volume_created = True
        docker("run", "-d", "--name", database, "--network", network,
               "-e", f"POSTGRES_PASSWORD={password}", "postgres:17-bookworm")
        database_created = True
        deadline = time.monotonic() + 60
        while True:
            try:
                docker("exec", database, "pg_isready", "-h", "127.0.0.1", "-U", "postgres", timeout=10)
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("隔离 PostgreSQL 未在 60 秒内就绪")
                time.sleep(1)
        checks = '''
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import bs4, httpx, psycopg, pydantic, rich
from ezbookkeeping_importer.adapters.persistence.postgres import SCHEMA
assert os.getuid() == 10001 and os.getgid() == 10001
assert Path.cwd() == Path('/app')
assert not Path('/app/data/config.toml').exists()
assert 'CREATE TABLE schema_version' in SCHEMA.read_text()
assert datetime(2026, 1, 1, tzinfo=ZoneInfo('Asia/Shanghai')).utcoffset() == timedelta(hours=8)
for kind in ('email', 'reports', 'logs'):
    directory = Path('/app/data', kind)
    directory.mkdir(exist_ok=True)
    (directory / 'smoke').write_text('persistent')
'''
        docker(*base, "--network", "none", *mounts, "--entrypoint", "python", image,
               "-c", checks)
        # 同一数据卷由新容器复用；CLI 默认配置路径与真实部署保持一致。
        invocation = [*base, "--network", network, *mounts, "-e",
                      f"EBKI_DATABASE_URL=postgresql://postgres:{password}@{database}/ebki",
                      image]
        for command in ("migrate", "migrate", "status"):
            print(docker(*invocation, command, timeout=60))
        docker(*base, "--network", "none", *mounts, "--entrypoint", "python", image,
               "-c", DEFAULT_CONFIG_CHECK + "\n"
               "assert all(Path('/app/data', k, 'smoke').read_text() == 'persistent' "
               "for k in ('email', 'reports', 'logs'))")
        # 空宿主目录通过真实入口创建；帮助、显式路径及只读错误不做隐藏回退。
        with tempfile.TemporaryDirectory(prefix=name) as directory:
            data = Path(directory)
            data.chmod(0o777)
            bind = ['--mount', f'type=bind,src={data},dst=/app/data']
            readonly = ['--mount', f'type=bind,src={data},dst=/app/data,readonly']
            docker(*base, '--network', 'none', *readonly, image, '--help')
            assert not (data / 'config.toml').exists()
            expect_configuration_error(*base, '--network', 'none', *bind, image,
                                       '--config', 'data/missing.toml', 'migrate',
                                       message='cannot be read')
            assert not (data / 'config.toml').exists()
            expect_configuration_error(*base, '--network', 'none', *readonly, image,
                                       'migrate', message='initialization failed (EROFS)')
            database_env = ['-e', f'EBKI_DATABASE_URL=postgresql://postgres:{password}@{database}/ebki']
            docker(*base, '--network', network, *bind, *database_env, image, 'migrate', timeout=60)
            docker(*base, '--network', 'none', *bind, '--entrypoint', 'python', image,
                   '-c', DEFAULT_CONFIG_CHECK)
            docker(*base, '--network', network, *readonly, *database_env, image, 'status', timeout=60)
            docker(*base, '--network', 'none', *bind, '--entrypoint', 'python', image,
                   '-c', "from pathlib import Path; "
                   "Path('/app/data/config.toml').write_text('invalid = [')")
            expect_configuration_error(*base, '--network', 'none', *bind, image,
                                       'migrate', message='invalid TOML syntax')
            docker(*base, '--network', 'none', *bind, '--entrypoint', 'python', image,
                   '-c', "from pathlib import Path; "
                   "assert Path('/app/data/config.toml').read_text() == 'invalid = ['; "
                   "Path('/app/data/config.toml').unlink()")
        # 初始默认值已验证，生命周期场景才定制为不依赖真实 AI 的合成规则。
        docker(*base, "--network", "none", *mounts, "--entrypoint", "python", image,
               "-c", "from pathlib import Path; "
               f"Path('/app/data/config.toml').write_text({SMOKE_CONFIG!r})")
        for command in ("migrate", "status"):
            docker(*invocation, command, timeout=60)
        docker(*base, "--network", "none", *mounts, "--entrypoint", "python", image,
               "-c", "from pathlib import Path; "
               f"assert Path('/app/data/config.toml').read_text() == {SMOKE_CONFIG!r}")
        verify_lifecycle(
            docker, image, platform_args, network, mounts,
            f"postgresql://postgres:{password}@{database}/ebki", f"{name}-app",
        )
        print(f"镜像验证通过：{image} ({platform or '本机架构'})")
    finally:
        # 清理失败也明确呈现，不把残留资源当作成功。
        cleanup_errors = []
        cleanup = []
        if database_created:
            cleanup.append(("rm", "-fv", database))
        if volume_created:
            cleanup.append(("volume", "rm", volume))
        if network_created:
            cleanup.append(("network", "rm", network))
        for cleanup_command in cleanup:
            try:
                docker(*cleanup_command)
            except subprocess.CalledProcessError as exc:
                cleanup_errors.append(exc.stderr)
        if cleanup_errors:
            raise RuntimeError("隔离资源清理失败：" + "\n".join(cleanup_errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"))
    args = parser.parse_args()
    try:
        verify(args.image, args.platform)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr) from error
