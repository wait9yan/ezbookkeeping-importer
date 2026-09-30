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

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable
# 确认 wheel 内 SQL 已解析符号链接，不依赖运行镜像中的构建源码。
RUN python -c 'from importlib.resources import files; from pathlib import Path; assert files("ezbookkeeping_importer.adapters.persistence").joinpath("schema.sql").read_bytes() == Path("migrations/001_initial.sql").read_bytes()'

FROM base AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
COPY --from=builder /app/.venv /app/.venv
RUN mkdir -p /app/data/email /app/data/reports /app/data/logs \
    && chown -R 10001:10001 /app/data
USER 10001:10001
ENTRYPOINT ["ebki"]
CMD ["run"]
