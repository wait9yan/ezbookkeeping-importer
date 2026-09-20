FROM ghcr.io/astral-sh/uv:0.11.21 AS uv
FROM python:3.12.13-slim-bookworm

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable \
    && mkdir -p /app/var/evidence /app/var/reports /app/var/logs \
    && chown -R 10001:10001 /app/var
USER 10001:10001
ENTRYPOINT ["ebki", "--config", "/app/config.toml"]
CMD ["worker"]
