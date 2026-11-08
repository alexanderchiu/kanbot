"""Command line entry point."""

import argparse
import os
from contextlib import suppress
from pathlib import Path

from kanbot import __doc__ as DESCRIPTION
from kanbot import __version__
from kanbot.db import Database
from kanbot.web import LOCAL_HOSTS, make_server


def default_database():
    env = os.environ.get
    return env("KANBOT_DATABASE_URL") or env("KANBOT_DB") or str(Path.cwd() / "kanban.db")


def main(argv=None):
    env = os.environ.get
    database = argparse.ArgumentParser(add_help=False)
    database.add_argument(
        "--db",
        default=default_database(),
        help="SQLite file or database URL, such as a Supabase postgresql:// URL "
        "(env KANBOT_DATABASE_URL, or KANBOT_DB; default: kanban.db here)",
    )
    # On a subcommand, --db can come before or after it.
    database_again = argparse.ArgumentParser(add_help=False)
    database_again.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(description=DESCRIPTION, parents=[database])
    parser.add_argument("--version", action="version", version=f"kanbot {__version__}")
    parser.add_argument(
        "--host",
        default=env("KANBOT_HOST", "127.0.0.1"),
        help="Address to listen on (env KANBOT_HOST). Use 0.0.0.0 in a container.",
    )
    parser.add_argument("--port", type=int, default=int(env("KANBOT_PORT", "8787")))
    parser.add_argument(
        "--backup-dir",
        default=env("KANBOT_BACKUP_DIR"),
        help="Where to keep SQLite backups (default: backups/ beside the database)",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=env("KANBOT_ALLOWED_HOSTS", ""),
        help="Comma-separated host names or IPs browsers may use to reach the board, besides "
        "localhost (env KANBOT_ALLOWED_HOSTS). * allows any, which removes DNS rebinding "
        "protection.",
    )
    parser.add_argument(
        "--no-migrate",
        action="store_true",
        default=env("KANBOT_MIGRATE", "1") == "0",
        help="Don't change the schema at startup; refuse to start if it's out of date. "
        "For setups where CI runs kanbot migrate (env KANBOT_MIGRATE=0).",
    )
    commands = parser.add_subparsers(dest="command", title="commands", metavar="")
    commands.add_parser(
        "migrate", parents=[database_again], help="Bring the database schema up to date, then exit"
    )
    copy = commands.add_parser(
        "copy-db",
        help="Copy everything from one database into a new, empty one",
        description="Copy every ticket, note, history entry and agent session from SOURCE "
        "into TARGET, keeping IDs and timestamps. TARGET must be empty. SOURCE is left as is, "
        "apart from bringing its schema up to date.",
    )
    copy.add_argument("source", help="SQLite file or database URL to copy from")
    copy.add_argument("target", help="SQLite file or database URL to copy into")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        db = Database(args.db)
        try:
            before, head = db.revisions()
            db.upgrade()
        finally:
            db.close()
        print(f"{db}: {'already at' if before == head else 'upgraded to'} revision {head}")
        return
    if args.command == "copy-db":
        from kanbot.copy import copy_database

        try:
            counts = copy_database(args.source, args.target)
        except ValueError as error:
            parser.exit(1, f"kanbot copy-db: {error}\n")
        print(", ".join(f"{count} {table}" for table, count in counts.items()), "copied")
        return

    db = Database(args.db)
    backup_dir = None
    if db.path:
        backup_dir = args.backup_dir or db.path.parent / "backups"
    try:
        server = make_server(
            db,
            args.port,
            backup_dir,
            args.host,
            args.allowed_hosts.split(","),
            migrate=not args.no_migrate,
        )
    except RuntimeError as error:
        parser.exit(1, f"kanbot: {error}\n")
    print(
        f"Kanbot {__version__} running on {args.host}:{server.server_port} (db: {db})", flush=True
    )
    if args.host not in LOCAL_HOSTS:
        print(f"Allowed hosts: {', '.join(sorted(server.allowed_hosts))}", flush=True)
    with suppress(KeyboardInterrupt):
        server.serve_forever()
