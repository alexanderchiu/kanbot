"""Command line entry point."""

import argparse
import os
from contextlib import suppress
from pathlib import Path

from kanbot import __doc__ as DESCRIPTION
from kanbot import __version__
from kanbot.web import LOCAL_HOSTS, make_server


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"kanbot {__version__}")
    env = os.environ.get
    parser.add_argument(
        "--host",
        default=env("KANBOT_HOST", "127.0.0.1"),
        help="Address to listen on (env KANBOT_HOST). Use 0.0.0.0 in a container.",
    )
    parser.add_argument("--port", type=int, default=int(env("KANBOT_PORT", "8787")))
    parser.add_argument(
        "--db", default=env("KANBOT_DB", str(Path.cwd() / "kanban.db")), help="SQLite database path"
    )
    parser.add_argument(
        "--backup-dir",
        default=env("KANBOT_BACKUP_DIR"),
        help="Where to keep backups (default: backups/ beside the database)",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=env("KANBOT_ALLOWED_HOSTS", ""),
        help="Comma-separated host names or IPs browsers may use to reach the board, besides "
        "localhost (env KANBOT_ALLOWED_HOSTS). * allows any, which removes DNS rebinding "
        "protection.",
    )
    args = parser.parse_args()
    backup_dir = args.backup_dir or Path(args.db).resolve().parent / "backups"
    server = make_server(args.db, args.port, backup_dir, args.host, args.allowed_hosts.split(","))
    print(
        f"Kanbot {__version__} running on {args.host}:{server.server_port} (db: {args.db})",
        flush=True,
    )
    if args.host not in LOCAL_HOSTS:
        print(f"Allowed hosts: {', '.join(sorted(server.allowed_hosts))}", flush=True)
    with suppress(KeyboardInterrupt):
        server.serve_forever()
