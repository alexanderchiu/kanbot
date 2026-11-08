"""Copy every ticket, note, dependency, history entry and agent session between databases."""

import sqlalchemy as sa

from kanbot.db import Database
from kanbot.schema import metadata

BATCH = 500


def copy_database(source, target):
    """Copy source into target, keeping IDs and timestamps. Both are brought up to the
    latest schema first, and target must be empty. Returns rows copied per table."""
    source = source if isinstance(source, Database) else Database(source)
    target = target if isinstance(target, Database) else Database(target)
    source.upgrade()
    target.upgrade()
    counts = {}
    with source.connect() as reader, target.transaction() as writer:
        for table in metadata.sorted_tables:
            if writer.connection.execute(sa.select(sa.func.count()).select_from(table)).scalar():
                raise ValueError(f"{target} already has {table.name}; copy into an empty database.")
        for table in metadata.sorted_tables:
            rows = [dict(row) for row in reader.connection.execute(sa.select(table)).mappings()]
            for start in range(0, len(rows), BATCH):
                writer.connection.execute(table.insert(), rows[start : start + BATCH])
            counts[table.name] = len(rows)
            if "id" in table.c:
                continue_ids(reader, writer, table.name)
    return counts


def last_id(conn, table):
    """The highest ID a table has handed out, counting deleted rows where the database
    remembers them."""
    highest = conn.scalar(f"SELECT MAX(id) FROM {table}") or 0
    if conn.connection.dialect.name == "sqlite":
        seq = conn.scalar("SELECT seq FROM sqlite_sequence WHERE name = :t", {"t": table})
    else:
        seq = conn.scalar(
            f"SELECT CASE WHEN is_called THEN last_value ELSE 0 END"
            f" FROM {conn.scalar('SELECT pg_get_serial_sequence(:t, :c)', {'t': table, 'c': 'id'})}"
        )
    return max(highest, seq or 0)


def continue_ids(reader, writer, table):
    """Make the target hand out IDs after the source's, so deleted IDs aren't reused."""
    high = last_id(reader, table)
    if not high:
        return
    if writer.connection.dialect.name == "sqlite":
        writer.execute("DELETE FROM sqlite_sequence WHERE name = :t", {"t": table})
        writer.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES (:t, :high)", {"t": table, "high": high}
        )
    else:
        writer.execute(
            "SELECT setval(pg_get_serial_sequence(:t, 'id'), :high)", {"t": table, "high": high}
        )
