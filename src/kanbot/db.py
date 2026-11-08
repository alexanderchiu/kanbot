"""Database access for SQLite and Postgres (including Supabase), with Alembic migrations."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from kanbot import legacy
from kanbot.schema import PRIORITIES, STATUSES

__all__ = ["PRIORITIES", "STATUSES", "RESOLVED_STATUSES", "Database", "now"]

RESOLVED_STATUSES = ("done", "cancelled")
BACKUPS_KEPT = 30
# The revision an unversioned SQLite file matches once legacy.upgrade has run on it.
BASELINE = "0001"
# Postgres advisory lock that serialises writes, like SQLite's BEGIN IMMEDIATE.
WRITE_LOCK = 0x6B616E626F74  # "kanbot"


def now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def database_url(target):
    """Turn a file path or a database URL into a SQLAlchemy URL."""
    target = str(target)
    if "://" not in target:
        return sa.URL.create("sqlite", database=str(Path(target).resolve()))
    url = sa.make_url(target)
    # Supabase and most hosts hand out postgres:// or postgresql:// URLs.
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+psycopg")
    return url


class Conn:
    """A connection that runs plain SQL with :named parameters and returns mappings."""

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, params=None):
        return self.connection.execute(sa.text(sql), params or {}).mappings()

    def executemany(self, sql, rows):
        if rows:
            self.connection.execute(sa.text(sql), rows)

    def rowcount(self, sql, params=None):
        """Run a statement and return how many rows it changed."""
        return self.connection.execute(sa.text(sql), params or {}).rowcount

    def scalar(self, sql, params=None):
        return self.connection.execute(sa.text(sql), params or {}).scalar()


class Database:
    def __init__(self, target):
        self.url = database_url(target)
        if self.url.get_backend_name() == "sqlite":
            self.engine = sa.create_engine(self.url, connect_args={"timeout": 10})
            sa.event.listen(self.engine, "connect", _sqlite_connect)
            sa.event.listen(self.engine, "begin", _sqlite_begin)
        else:
            self.engine = sa.create_engine(
                self.url,
                # Hosted databases drop idle connections, and transaction-mode poolers
                # such as Supabase's port 6543 can't keep prepared statements.
                pool_pre_ping=True,
                pool_recycle=300,
                pool_size=5,
                max_overflow=5,
                connect_args={"prepare_threshold": None},
            )

    @property
    def is_sqlite(self):
        return self.engine.dialect.name == "sqlite"

    @property
    def path(self):
        """The file of a SQLite database, otherwise None."""
        return Path(self.url.database) if self.is_sqlite and self.url.database else None

    def __str__(self):
        return str(self.path) if self.path else self.url.render_as_string(hide_password=True)

    def close(self):
        self.engine.dispose()

    @contextmanager
    def connect(self):
        """A connection for reads."""
        with self.engine.connect() as connection:
            yield Conn(connection)

    @contextmanager
    def transaction(self):
        """A transaction that holds the write lock, so concurrent agents can't interleave
        changes such as column renumbering."""
        connection = self.engine.connect().execution_options(kanbot_write=True)
        with connection, connection.begin():
            if not self.is_sqlite:
                connection.execute(
                    sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": WRITE_LOCK}
                )
            yield Conn(connection)

    def alembic_config(self, connection=None):
        config = Config()
        config.set_main_option("script_location", str(resources.files("kanbot") / "migrations"))
        config.attributes["connection"] = connection
        return config

    def revisions(self):
        """The current and latest revisions. Current is None for a new database, and
        "legacy" for a SQLite file from before Alembic."""
        head = ScriptDirectory.from_config(self.alembic_config()).get_current_head()
        with self.engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
            if current is None and self.is_sqlite and sa.inspect(connection).has_table("tickets"):
                current = "legacy"
        return current, head

    def check_schema(self):
        """For servers that leave migrations to CI: raise if the schema is behind this
        version of Kanbot. Return a warning if it's ahead, or None if it matches."""
        current, head = self.revisions()
        if current == head:
            return None
        script = ScriptDirectory.from_config(self.alembic_config())
        known = current not in (None, "legacy") and current in {
            r.revision for r in script.walk_revisions()
        }
        if current in (None, "legacy") or known:
            raise RuntimeError(
                f"{self} is at revision {current or 'none'}, but this Kanbot needs {head}."
                " Run kanbot migrate, or start without --no-migrate."
            )
        return f"{self} is at revision {current}, newer than this Kanbot ({head}); carrying on."

    def needs_upgrade(self):
        current, head = self.revisions()
        return current != head

    def upgrade(self):
        """Bring the schema up to date. Safe to run every start, and by several servers
        at once on Postgres, where the write lock makes them take turns."""
        if self.revisions()[0] == "legacy":
            legacy.upgrade(self.path)
            with self.transaction() as conn:
                command.stamp(self.alembic_config(conn.connection), BASELINE)
        with self.transaction() as conn:
            command.upgrade(self.alembic_config(conn.connection), "head")

    def backup(self, backup_dir, keep=BACKUPS_KEPT, force=False):
        """Copy a SQLite database into backup_dir, unless it's unchanged since the last
        copy. Postgres databases are backed up by their host, so this returns None."""
        db = self.path
        if db is None or not db.exists():
            return None
        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        pattern = f"{db.stem}-*.db"
        existing = sorted(backup_dir.glob(pattern))
        if not force and existing and existing[-1].stat().st_mtime >= db.stat().st_mtime:
            return None

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"{db.stem}-{stamp}.db"
        source, copy = sqlite3.connect(db, timeout=10), sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            source.close()
            copy.close()
        for old in sorted(backup_dir.glob(pattern))[:-keep]:
            old.unlink()
        return target


def _sqlite_connect(dbapi_connection, _record):
    # Let the begin listener issue BEGIN itself, instead of the sqlite3 module.
    dbapi_connection.isolation_level = None


def _sqlite_begin(connection):
    write = connection.get_execution_options().get("kanbot_write")
    connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")
