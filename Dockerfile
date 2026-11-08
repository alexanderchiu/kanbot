# Kanbot has no third-party dependencies, so the image is just Python and two files.
FROM python:3.14-slim

WORKDIR /app
COPY server.py index.html ./

ENV KANBOT_HOST=0.0.0.0 \
    KANBOT_PORT=8787 \
    KANBOT_DB=/data/kanban.db \
    KANBOT_BACKUP_DIR=/data/backups \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8787

# localhost is always an allowed host, so the check works whatever KANBOT_ALLOWED_HOSTS is.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/api', timeout=4)"

CMD ["python", "server.py"]
