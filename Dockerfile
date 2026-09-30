# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv
FROM python:3.12.13-slim-bookworm AS base

FROM base AS builder

COPY --from=uv /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock hatch_build.py ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable
# 确认 wheel 内 SQL 与全链契约已解析，不依赖运行镜像中的构建源码。
RUN python -c 'from importlib.resources import files; from pathlib import Path; assert files("ezbookkeeping_importer.adapters.persistence").joinpath("schema.sql").read_bytes() == Path("migrations/001_initial.sql").read_bytes(); from ezbookkeeping_importer.adapters.persistence.migrations import load_migrations; load_migrations()'

FROM base AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/ebki/bin:/app/.venv/bin:$PATH"
COPY --from=builder /app/.venv /app/.venv
RUN apt-get update && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*
COPY --chmod=755 docker/ebki /opt/ebki/bin/ebki
ENTRYPOINT ["ebki"]
CMD ["run"]
