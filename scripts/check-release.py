#!/usr/bin/env python3
"""校验版本并使用 GHCR 明确的 404 响应确认正式标签不存在。"""
import argparse
import base64
import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def release_version(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("发布标签必须为 vX.Y.Z")
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    if tag[1:] != version:
        raise ValueError("发布标签与 pyproject.toml 版本不一致")
    return version


def assert_absent(image: str, version: str) -> None:
    repository = image.removeprefix("ghcr.io/")
    if image != f"ghcr.io/{repository}" or not re.fullmatch(r"[a-z0-9_./-]+", repository):
        raise ValueError("仅支持小写 GHCR 镜像路径")
    credentials = base64.b64encode(
        f"{os.environ['GITHUB_ACTOR']}:{os.environ['GH_TOKEN']}".encode()
    ).decode()
    query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{repository}:pull"})
    request = urllib.request.Request(
        f"https://ghcr.io/token?{query}", headers={"Authorization": f"Basic {credentials}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{repository}/manifests/{version}", method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": ", ".join((
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ))},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return
        raise
    raise ValueError(f"正式版本 {version} 已存在，拒绝覆盖")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--image")
    args = parser.parse_args()
    version = release_version(args.tag)
    if args.image:
        assert_absent(args.image, version)
    print(version)
