"""Alembic environment.

Kanbot runs migrations itself at startup, passing its connection in
config.attributes["connection"]. To use the alembic command instead, for example to draft
a migration, set KANBOT_DATABASE_URL or pass -x url=... (see alembic.ini).
"""

import os

from alembic import context

from kanbot.db import Database
from kanbot.schema import metadata

config = context.config


def run(connection):
    context.configure(
        connection=connection,
        target_metadata=metadata,
        # SQLite can't alter most things in place, so changes rebuild the table.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    run(connection)
else:
    url = context.get_x_argument(as_dictionary=True).get("url") or os.environ.get(
        "KANBOT_DATABASE_URL", "kanban.db"
    )
    if context.is_offline_mode():
        raise SystemExit("Offline mode isn't supported; run against a database.")
    database = Database(url)
    try:
        with database.transaction() as conn:
            run(conn.connection)
    finally:
        database.close()
