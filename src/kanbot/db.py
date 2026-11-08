"""SQLite connection, schema, migrations and backups."""

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

STATUSES = ("follow_up", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("high", "medium", "low")
BACKUPS_KEPT = 30

TICKETS_TABLE = """
CREATE TABLE {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('follow_up', 'in_progress', 'blocked', 'done', 'cancelled')),
    priority    TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    project     TEXT,
    idea        INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    assignee    TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    completed_at TEXT
);
"""  # noqa: E501

NOTES_TABLE = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    author     TEXT,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    actor     TEXT,
    field     TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    at        TEXT NOT NULL
);
"""
DEPENDENCIES_TABLE = """
CREATE TABLE IF NOT EXISTS dependencies (
    ticket_id  INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    PRIMARY KEY (ticket_id, depends_on)
);
"""
RESOLVED_STATUSES = ("done", "cancelled")


def now():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(db_path):
    """Serialise writes, so concurrent agents can't interleave column renumbering."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def tickets_table_outdated(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tickets'").fetchone()
    if row is None:
        return False
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    return not (
        {"idea", "created_by", "assignee", "completed_at"} <= columns
        and all(f"'{status}'" in row["sql"] for status in STATUSES)
    )


def migrate(db_path):
    """Create the tickets table, or bring an older one up to date.

    SQLite can't change a CHECK constraint in place, so a table that predates the
    current schema is rebuilt. This runs in one transaction, so a failure leaves the
    old table untouched. Tickets already done get updated_at as their completion time,
    the closest record available.
    """
    with transaction(db_path) as conn:
        conn.execute(NOTES_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS notes_ticket ON notes (ticket_id)")
        conn.execute(DEPENDENCIES_TABLE)
        conn.execute(HISTORY_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS history_ticket ON history (ticket_id)")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tickets'").fetchone()
        if row is None:
            conn.execute(TICKETS_TABLE.format(name="tickets"))
            return
        if not tickets_table_outdated(conn):
            return

        columns = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(tickets)"))
        # Keep the ID counter, so IDs of deleted tickets are never reused.
        counter = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'tickets'").fetchone()
        conn.execute(TICKETS_TABLE.format(name="tickets_new"))
        conn.execute(f"INSERT INTO tickets_new ({columns}) SELECT {columns} FROM tickets")
        conn.execute("DROP TABLE tickets")
        conn.execute("ALTER TABLE tickets_new RENAME TO tickets")
        conn.execute(
            "UPDATE tickets SET completed_at = updated_at"
            " WHERE status = 'done' AND completed_at IS NULL"
        )
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tickets'")
        if counter:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('tickets', ?)", (counter["seq"],)
            )


def backup_database(db_path, backup_dir, keep=BACKUPS_KEPT, force=False):
    """Copy the database into backup_dir, unless it's unchanged since the last copy."""
    db = Path(db_path)
    if not db.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    pattern = f"{db.stem}-*.db"
    existing = sorted(backup_dir.glob(pattern))
    if not force and existing and existing[-1].stat().st_mtime >= db.stat().st_mtime:
        return None

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db.stem}-{stamp}.db"
    source, copy = connect(db), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        source.close()
        copy.close()
    for old in sorted(backup_dir.glob(pattern))[:-keep]:
        old.unlink()
    return target
