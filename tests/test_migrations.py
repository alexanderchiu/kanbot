import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from backends import PostgresBackend, SqliteBackend

from kanbot.cli import main
from kanbot.copy import copy_database
from kanbot.db import Database
from kanbot.schema import metadata
from kanbot.web import make_server


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    with urllib.request.urlopen(request) as response:
        return json.load(response)


class Serving:
    """Run a server on a database for the length of a with block."""

    def __init__(self, database):
        self.server = make_server(database, 0)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}"

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


class MigrationsTest(SqliteBackend, unittest.TestCase):
    def setUp(self):
        self.db = self.new_database()
        self.database = Database(self.db)
        self.addCleanup(self.database.close)

    def test_new_database_is_at_head_and_matches_schema(self):
        self.assertEqual(self.database.revisions(), (None, "0001"))
        self.database.upgrade()
        self.assertEqual(self.database.revisions(), ("0001", "0001"))
        self.assertFalse(self.database.needs_upgrade())
        with self.database.engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), metadata)
        self.assertEqual(diff, [], "the migrations and kanbot/schema.py disagree")

    def test_upgrade_is_safe_to_repeat_and_downgrade_removes_everything(self):
        self.database.upgrade()
        self.database.upgrade()
        with self.database.transaction() as conn:
            command.downgrade(self.database.alembic_config(conn.connection), "base")
        tables = sa.inspect(self.database.engine).get_table_names()
        self.assertEqual(set(tables) & set(metadata.tables), set())

    def test_no_migrate_checks_the_schema_instead(self):
        with self.assertRaisesRegex(RuntimeError, "kanbot migrate"):
            make_server(self.db, 0, migrate=False)
        self.assertEqual(self.database.revisions()[0], None, "it mustn't migrate")
        self.database.upgrade()
        make_server(self.db, 0, migrate=False).server_close()
        # A newer Kanbot, for example deployed by CI, migrated ahead of this one.
        self.sql("UPDATE alembic_version SET version_num = 'ffff'")
        self.assertIn("newer than this Kanbot", self.database.check_schema())
        make_server(self.db, 0, migrate=False).server_close()

    def test_concurrent_upgrades_take_turns(self):
        errors = []

        def upgrade():
            database = Database(self.db)
            try:
                database.upgrade()
            except Exception as error:
                errors.append(error)
            finally:
                database.close()

        threads = [threading.Thread(target=upgrade) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.database.revisions()[0], "0001")


class PostgresMigrationsTest(PostgresBackend, MigrationsTest):
    def test_row_level_security_blocks_the_supabase_data_api(self):
        self.database.upgrade()
        rows = self.sql(
            "SELECT relname, relrowsecurity FROM pg_class"
            " WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
        )
        self.assertEqual(
            dict(rows), dict.fromkeys([*metadata.tables, "alembic_version"], True), rows
        )


class LegacyFileTest(unittest.TestCase):
    def test_unversioned_file_is_upgraded_and_stamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                " description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,"
                " priority TEXT NOT NULL, project TEXT, position INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO tickets (title, status, priority, created_at, updated_at)"
                " VALUES ('kept', 'follow_up', 'low', 'then', 'then')"
            )
            conn.commit()
            conn.close()
            database = Database(path)
            try:
                self.assertEqual(database.revisions(), ("legacy", "0001"))
                self.assertTrue(database.needs_upgrade())
                database.upgrade()
                self.assertEqual(database.revisions(), ("0001", "0001"))
            finally:
                database.close()
            with Serving(path) as base:
                [ticket] = call(base, "GET", "/api/tickets")
            self.assertEqual(ticket["title"], "kept")


class CopyTest(SqliteBackend, unittest.TestCase):
    """Copy a SQLite board into the backend under test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.source = Path(tmp.name) / "source.db"
        self.db = self.new_database()

    def fill_source(self):
        with Serving(self.source) as base:
            first = call(base, "POST", "/api/tickets", {"title": "First", "created_by": "a"})
            gone = call(base, "POST", "/api/tickets", {"title": "Deleted"})
            call(
                base,
                "POST",
                "/api/tickets",
                {"title": "Second", "idea": True, "depends_on": [first["id"]], "status": "done"},
            )
            call(base, "POST", f"/api/tickets/{first['id']}/notes", {"body": "A note"})
            call(base, "PATCH", f"/api/tickets/{first['id']}", {"priority": "high", "actor": "b"})
            call(base, "DELETE", f"/api/tickets/{gone['id']}")
            # A deleted ticket with the highest ID: the copy mustn't hand that ID out again.
            last = call(base, "POST", "/api/tickets", {"title": "Deleted last"})
            call(base, "DELETE", f"/api/tickets/{last['id']}")
            call(
                base,
                "POST",
                "/api/agent-events",
                {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": "/w/app"},
            )
            return last["id"], self.snapshot(base)

    def snapshot(self, base):
        tickets = call(base, "GET", "/api/tickets")
        details = [call(base, "GET", f"/api/tickets/{t['id']}") for t in tickets]
        agents = call(base, "GET", "/api/agents?all=true")
        for session in agents:
            for key in ("age_seconds", "idle_seconds", "state_seconds", "working_seconds"):
                session.pop(key)
        return details, agents

    def test_copies_everything_and_keeps_ids(self):
        deleted_id, before = self.fill_source()
        counts = copy_database(self.source, self.db)
        self.assertEqual(counts["tickets"], 2)
        self.assertEqual(counts["notes"], 1)
        with Serving(self.db) as base:
            self.assertEqual(self.snapshot(base), before)
            new = call(base, "POST", "/api/tickets", {"title": "After the move"})
            self.assertEqual(new["id"], deleted_id + 1)
            note = call(base, "POST", f"/api/tickets/{new['id']}/notes", {"body": "x"})
            self.assertEqual(note["id"], 2)

    def test_refuses_a_target_that_has_tickets(self):
        self.fill_source()
        copy_database(self.source, self.db)
        with self.assertRaisesRegex(ValueError, "empty database"):
            copy_database(self.source, self.db)


class PostgresCopyTest(PostgresBackend, CopyTest):
    pass


class CliTest(unittest.TestCase):
    def run_cli(self, *args):
        out = StringIO()
        with redirect_stdout(out):
            main(list(args))
        return out.getvalue()

    def test_migrate_and_copy_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, target = Path(tmp) / "a.db", Path(tmp) / "b.db"
            self.assertIn("upgraded to revision 0001", self.run_cli("migrate", "--db", str(source)))
            # --db also works before the command.
            self.assertIn("already at revision 0001", self.run_cli("--db", str(source), "migrate"))
            self.assertIn("0 tickets", self.run_cli("copy-db", str(source), str(target)))
            conn = sqlite3.connect(target)
            conn.execute(
                "INSERT INTO tickets (title, status, priority, created_at, updated_at)"
                " VALUES ('x', 'done', 'low', 'now', 'now')"
            )
            conn.commit()
            conn.close()
            with self.assertRaises(SystemExit) as raised:
                self.run_cli("copy-db", str(source), str(target))
            self.assertEqual(raised.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
