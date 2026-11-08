import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from kanbot.db import backup_database
from kanbot.web import make_server


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "board.db"
        self.backups = Path(self.tmp.name) / "backups"

    def write(self, value):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE IF NOT EXISTS t (v)")
        conn.execute("INSERT INTO t VALUES (?)", (value,))
        conn.commit()
        conn.close()

    def test_no_database_means_no_backup(self):
        self.assertIsNone(backup_database(self.db, self.backups))

    def test_copies_data_and_skips_unchanged_database(self):
        self.write("a")
        target = backup_database(self.db, self.backups)
        conn = sqlite3.connect(target)
        self.assertEqual(conn.execute("SELECT v FROM t").fetchall(), [("a",)])
        conn.close()
        self.assertIsNone(backup_database(self.db, self.backups))
        os.utime(self.db, (target.stat().st_mtime + 5,) * 2)
        self.assertIsNotNone(backup_database(self.db, self.backups))

    def test_keeps_only_newest_and_ignores_other_files(self):
        self.write("a")
        self.backups.mkdir()
        (self.backups / "notes.txt").write_text("mine")
        for day in (1, 2, 3):
            (self.backups / f"board-2020010{day}-000000.db").touch()
            os.utime(self.backups / f"board-2020010{day}-000000.db", (1, 1))
        newest = backup_database(self.db, self.backups, keep=2)
        names = sorted(path.name for path in self.backups.iterdir())
        self.assertEqual(names, ["board-20200103-000000.db", newest.name, "notes.txt"])

    def test_backup_is_taken_before_migrating(self):
        self.write("a")
        make_server(self.db, 0, self.backups).server_close()
        [target] = self.backups.iterdir()
        conn = sqlite3.connect(target)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        conn.close()
        self.assertEqual(tables, ["t"])


class MigrationTest(unittest.TestCase):
    OLD_TABLE = (
        "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
        " description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, priority TEXT NOT NULL,"
        " project TEXT, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL{extra})"
    )

    def test_keeps_real_completion_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(self.OLD_TABLE.format(extra=", completed_at TEXT"))
            conn.execute(
                "INSERT INTO tickets (title, status, priority, created_at, updated_at, completed_at)"
                " VALUES ('x', 'done', 'low', 'then', 'edited-later', 'real-completion')"
            )
            conn.commit()
            conn.close()
            make_server(db, 0).server_close()
            conn = sqlite3.connect(db)
            self.assertEqual(
                conn.execute("SELECT completed_at FROM tickets").fetchone(), ("real-completion",)
            )
            conn.close()

    def test_rebuilds_any_table_missing_part_of_the_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(self.OLD_TABLE.format(extra=", assignee TEXT"))
            conn.commit()
            conn.close()
            make_server(db, 0).server_close()
            conn = sqlite3.connect(db)
            columns = [row[1] for row in conn.execute("PRAGMA table_info(tickets)")]
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tickets'").fetchone()[0]
            conn.close()
            self.assertIn("completed_at", columns)
            self.assertIn("'blocked'", sql)

    def test_migration_is_backed_up_even_if_file_looks_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            db, backups = Path(tmp) / "old.db", Path(tmp) / "backups"
            backups.mkdir()
            (backups / "old-20200101-000000.db").touch()
            conn = sqlite3.connect(db)
            conn.execute(self.OLD_TABLE.format(extra=""))
            conn.commit()
            conn.close()
            os.utime(db, (1, 1))
            make_server(db, 0, backups).server_close()
            copies = [path for path in backups.iterdir() if path.stat().st_size]
            self.assertEqual(len(copies), 1)
            conn = sqlite3.connect(copies[0])
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tickets'").fetchone()[0]
            conn.close()
            self.assertNotIn("assignee", sql)

    def test_adds_idea_column_and_keeps_existing_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                " description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, priority TEXT NOT NULL,"
                " project TEXT, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO tickets (title, status, priority, created_at, updated_at)"
                " VALUES ('old', 'done', 'low', 'then', 'then')"
            )
            conn.commit()
            conn.close()

            # Starting twice checks that the migration is safe to repeat.
            make_server(db, 0).server_close()
            server = make_server(db, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_port}/api/tickets"
            try:
                with urllib.request.urlopen(url) as response:
                    [ticket] = json.load(response)
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual(ticket["title"], "old")
            self.assertIs(ticket["idea"], False)


    def test_backfills_completed_at_for_done_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                " description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, priority TEXT NOT NULL,"
                " project TEXT, idea INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO tickets (title, status, priority, created_at, updated_at) VALUES"
                " ('finished', 'done', 'low', 'then', 'finish-time'),"
                " ('open', 'follow_up', 'low', 'then', 'edit-time')"
            )
            conn.commit()
            conn.close()

            make_server(db, 0).server_close()
            conn = sqlite3.connect(db)
            rows = dict(conn.execute("SELECT title, completed_at FROM tickets"))
            conn.close()
            self.assertEqual(rows, {"finished": "finish-time", "open": None})

    def test_rebuild_allows_cancelled_and_preserves_data_and_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old.db"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                " description TEXT NOT NULL DEFAULT '',"
                " status TEXT NOT NULL CHECK (status IN ('follow_up', 'in_progress', 'done')),"
                " priority TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),"
                " project TEXT, position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,"
                " updated_at TEXT NOT NULL, idea INTEGER NOT NULL DEFAULT 0)"
            )
            for title, idea in (("first", 1), ("deleted", 0)):
                conn.execute(
                    "INSERT INTO tickets (title, status, priority, project, idea, position,"
                    " created_at, updated_at)"
                    " VALUES (?, 'in_progress', 'high', 'web', ?, 4, 'then', 'later')",
                    (title, idea),
                )
            conn.execute("DELETE FROM tickets WHERE title = 'deleted'")
            conn.commit()
            conn.close()

            server = make_server(db, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{server.server_port}/api/tickets"

            def call(method, url, body=None):
                data = json.dumps(body).encode() if body else None
                request = urllib.request.Request(url, data=data, method=method)
                with urllib.request.urlopen(request) as response:
                    return json.load(response)

            try:
                [old] = call("GET", base)
                self.assertEqual(
                    {k: old[k] for k in old},
                    {
                        "id": 1, "title": "first", "description": "", "status": "in_progress",
                        "priority": "high", "project": "web", "idea": True, "position": 4,
                        "created_by": None, "assignee": None, "note_count": 0,
                        "depends_on": [], "blocked_by": [], "blocks": [],
                        "created_at": "then", "updated_at": "later", "completed_at": None,
                    },
                )
                self.assertEqual(call("POST", base, {"title": "new"})["id"], 3)
                cancelled = call("PATCH", f"{base}/1", {"status": "cancelled"})
                self.assertEqual(cancelled["status"], "cancelled")
                blocked = call("PATCH", f"{base}/1", {"status": "blocked"})
                self.assertEqual(blocked["status"], "blocked")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
