# Kanbot has no third-party dependencies, so the image is Python, uv's install of the
# package, and nothing else.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /bin/

WORKDIR /app
# Use the image's Python, and copy files into the venv so it doesn't point back at a cache.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install the locked dependencies first, so editing the source doesn't redo this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project --no-editable

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH" \
    KANBOT_HOST=0.0.0.0 \
    KANBOT_PORT=8787 \
    KANBOT_DB=/data/kanban.db \
    KANBOT_BACKUP_DIR=/data/backups \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8787

# localhost is always an allowed host, so the check works whatever KANBOT_ALLOWED_HOSTS is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/api', timeout=4)"

CMD ["kanbot"]
