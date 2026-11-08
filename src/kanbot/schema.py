"""Table definitions, matching the latest migration.

Migrations in kanbot/migrations change the real schema; this module describes the result.
Alembic compares against it to draft new migrations (alembic revision --autogenerate),
and copy-db uses it to copy tables in order. Queries are plain SQL in each module.
"""

import sqlalchemy as sa

STATUSES = ("follow_up", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("high", "medium", "low")

# 64-bit IDs on Postgres. SQLite only treats INTEGER PRIMARY KEY as the row ID, and its
# integers are 64-bit anyway.
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def one_of(column, values):
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


metadata = sa.MetaData()

tickets = sa.Table(
    "tickets",
    metadata,
    sa.Column("id", Id, primary_key=True, autoincrement=True),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("description", sa.Text, nullable=False, server_default=""),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("priority", sa.Text, nullable=False),
    sa.Column("project", sa.Text),
    sa.Column("idea", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_by", sa.Text),
    sa.Column("assignee", sa.Text),
    sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("completed_at", sa.Text),
    sa.CheckConstraint(one_of("status", STATUSES), name="tickets_status"),
    sa.CheckConstraint(one_of("priority", PRIORITIES), name="tickets_priority"),
    # Never reuse the IDs of deleted tickets.
    sqlite_autoincrement=True,
)

notes = sa.Table(
    "notes",
    metadata,
    sa.Column("id", Id, primary_key=True, autoincrement=True),
    sa.Column("ticket_id", sa.BigInteger, nullable=False),
    sa.Column("author", sa.Text),
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Index("notes_ticket", "ticket_id"),
    sqlite_autoincrement=True,
)

dependencies = sa.Table(
    "dependencies",
    metadata,
    sa.Column("ticket_id", sa.BigInteger, primary_key=True),
    sa.Column("depends_on", sa.BigInteger, primary_key=True),
)

history = sa.Table(
    "history",
    metadata,
    sa.Column("id", Id, primary_key=True, autoincrement=True),
    sa.Column("ticket_id", sa.BigInteger, nullable=False),
    sa.Column("actor", sa.Text),
    sa.Column("field", sa.Text, nullable=False),
    sa.Column("old_value", sa.Text),
    sa.Column("new_value", sa.Text),
    sa.Column("at", sa.Text, nullable=False),
    sa.Index("history_ticket", "ticket_id"),
    sqlite_autoincrement=True,
)

agent_sessions = sa.Table(
    "agent_sessions",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("agent_id", sa.Text),
    sa.Column("agent_type", sa.Text),
    sa.Column("parent_key", sa.Text),
    sa.Column("model", sa.Text),
    sa.Column("cwd", sa.Text),
    sa.Column("project", sa.Text),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_detail", sa.Text),
    sa.Column("state_since", sa.Text, nullable=False),
    sa.Column("started_at", sa.Text, nullable=False),
    sa.Column("last_event_at", sa.Text, nullable=False),
    sa.Column("ended_at", sa.Text),
    sa.Column("working_since", sa.Text),
    sa.Column("working_seconds", sa.Float, nullable=False, server_default="0"),
    sa.Column("turns", sa.Integer, nullable=False, server_default="0"),
    sa.Column("tool_calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_tool", sa.Text),
    sa.Column("input_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("output_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("cache_read_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("cache_creation_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("ticket_id", sa.BigInteger),
    sa.Column("state_ts", sa.Float),
    sa.Index("agent_parent", "parent_key"),
)
