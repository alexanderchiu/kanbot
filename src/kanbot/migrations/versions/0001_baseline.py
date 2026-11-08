"""Baseline: tickets, notes, dependencies, history and agent sessions

SQLite files from before Alembic are upgraded to this schema by kanbot.legacy and then
stamped with this revision, so this migration only runs on new databases.

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = ("tickets", "notes", "dependencies", "history", "agent_sessions")
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade():
    op.create_table(
        "tickets",
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
        sa.CheckConstraint(
            "status IN ('follow_up', 'in_progress', 'blocked', 'done', 'cancelled')",
            name="tickets_status",
        ),
        sa.CheckConstraint("priority IN ('high', 'medium', 'low')", name="tickets_priority"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "notes",
        sa.Column("id", Id, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.BigInteger, nullable=False),
        sa.Column("author", sa.Text),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.Text, nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("notes_ticket", "notes", ["ticket_id"])
    op.create_table(
        "dependencies",
        sa.Column("ticket_id", sa.BigInteger, primary_key=True),
        sa.Column("depends_on", sa.BigInteger, primary_key=True),
    )
    op.create_table(
        "history",
        sa.Column("id", Id, primary_key=True, autoincrement=True),
        sa.Column("ticket_id", sa.BigInteger, nullable=False),
        sa.Column("actor", sa.Text),
        sa.Column("field", sa.Text, nullable=False),
        sa.Column("old_value", sa.Text),
        sa.Column("new_value", sa.Text),
        sa.Column("at", sa.Text, nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("history_ticket", "history", ["ticket_id"])
    op.create_table(
        "agent_sessions",
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
    )
    op.create_index("agent_parent", "agent_sessions", ["parent_key"])

    if op.get_bind().dialect.name == "postgresql":
        # Supabase serves tables in the public schema through its Data API, where its
        # default grants would let anyone holding the project's anon key read and write
        # them. Row level security with no policies shuts that out. Kanbot connects as
        # the tables' owner, which RLS doesn't apply to.
        for table in (*TABLES, "alembic_version"):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def downgrade():
    for table in reversed(TABLES):
        op.drop_table(table)
