import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server import backup_database, make_server


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.server = make_server(self.db, 0)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def create(self, **fields):
        fields.setdefault("title", "Ticket")
        status, ticket = self.call("POST", "/api/tickets", fields)
        self.assertEqual(status, 201, ticket)
        return ticket

    def titles(self, status):
        _, tickets = self.call("GET", f"/api/tickets?status={status}")
        return [t["title"] for t in tickets]

    def test_create_applies_defaults_and_normalizes_project(self):
        ticket = self.create(title="  Do it  ", project=" Website ")
        self.assertEqual(ticket["title"], "Do it")
        self.assertEqual(ticket["status"], "follow_up")
        self.assertEqual(ticket["priority"], "medium")
        self.assertEqual(ticket["project"], "website")

    def test_move_to_done_and_edit_fields(self):
        ticket = self.create()
        path = f"/api/tickets/{ticket['id']}"
        status, moved = self.call("PATCH", path, {"status": "done"})
        self.assertEqual((status, moved["status"]), (200, "done"))
        _, edited = self.call(
            "PATCH", path, {"title": "New", "priority": "high", "project": None}
        )
        self.assertEqual(edited["title"], "New")
        self.assertEqual(edited["priority"], "high")
        self.assertIsNone(edited["project"])
        self.assertEqual(edited["status"], "done")

    def test_new_tickets_are_placed_by_priority(self):
        self.create(title="low", priority="low")
        self.create(title="med1")
        self.create(title="high", priority="high")
        self.create(title="med2")
        self.assertEqual(self.titles("follow_up"), ["high", "med1", "med2", "low"])

    def test_explicit_position_overrides_priority(self):
        a = self.create(title="a", priority="high")
        self.create(title="b", priority="high")
        c = self.create(title="c", priority="low")
        self.call("PATCH", f"/api/tickets/{c['id']}", {"position": 0})
        self.assertEqual(self.titles("follow_up"), ["c", "a", "b"])
        self.call("PATCH", f"/api/tickets/{a['id']}", {"position": 999})
        self.assertEqual(self.titles("follow_up"), ["c", "b", "a"])

    def test_filters(self):
        self.create(title="Fix 100% bug", project="web", priority="high")
        self.create(title="Other", project="api", description="fix later")
        self.create(title="Fix thing", status="done")

        def titles(query):
            _, tickets = self.call("GET", f"/api/tickets?{query}")
            return {t["title"] for t in tickets}

        self.assertEqual(titles("project=WEB"), {"Fix 100% bug"})
        self.assertEqual(titles("priority=high"), {"Fix 100% bug"})
        self.assertEqual(titles("status=done"), {"Fix thing"})
        self.assertEqual(titles("q=fix"), {"Fix 100% bug", "Other", "Fix thing"})
        self.assertEqual(titles("q=100%25"), {"Fix 100% bug"})
        self.assertEqual(titles("q=%25"), {"Fix 100% bug"})

    def test_projects_endpoint(self):
        self.create(project="web")
        self.create(project="Web")
        self.create(project="api")
        self.create()
        _, projects = self.call("GET", "/api/projects")
        self.assertEqual(
            projects, [{"name": "api", "tickets": 1}, {"name": "web", "tickets": 2}]
        )

    def test_idea_tag(self):
        plain = self.create(title="task", project="web")
        self.assertIs(plain["idea"], False)
        idea = self.create(title="idea", project="web", idea=True)
        self.assertIs(idea["idea"], True)

        def titles(query):
            return [t["title"] for t in self.call("GET", f"/api/tickets?{query}")[1]]

        self.assertEqual(titles("idea=true"), ["idea"])
        self.assertEqual(titles("idea=false&project=web"), ["task"])
        _, updated = self.call("PATCH", f"/api/tickets/{plain['id']}", {"idea": True})
        self.assertIs(updated["idea"], True)
        self.assertIs(self.call("GET", f"/api/tickets/{plain['id']}")[1]["idea"], True)
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "x", "idea": 1})[0], 400)
        self.assertEqual(self.call("GET", "/api/tickets?idea=maybe")[0], 400)

    def test_cancel_and_reopen(self):
        ticket = self.create(title="dropped", project="web")
        self.create(title="kept", project="web")
        path = f"/api/tickets/{ticket['id']}"
        status, cancelled = self.call("PATCH", path, {"status": "cancelled"})
        self.assertEqual((status, cancelled["status"]), (200, "cancelled"))
        self.assertEqual(self.titles("cancelled"), ["dropped"])
        self.assertEqual(self.titles("follow_up"), ["kept"])
        self.assertEqual(self.call("GET", path)[0], 200)
        self.assertEqual(self.call("PATCH", path, {"status": "follow_up"})[0], 200)
        self.assertEqual(self.titles("cancelled"), [])

    def test_completed_at_follows_done_status(self):
        ticket = self.create()
        self.assertTrue(ticket["created_at"])
        self.assertIsNone(ticket["completed_at"])
        path = f"/api/tickets/{ticket['id']}"

        _, done = self.call("PATCH", path, {"status": "done"})
        self.assertTrue(done["completed_at"])
        self.assertEqual(done["created_at"], ticket["created_at"])

        # Editing a finished ticket doesn't change when it was completed.
        with self.subTest("edit keeps completion time"):
            conn = sqlite3.connect(self.db)
            conn.execute("UPDATE tickets SET completed_at = 'earlier'")
            conn.commit()
            conn.close()
            _, edited = self.call("PATCH", path, {"title": "Edited"})
            self.assertEqual(edited["completed_at"], "earlier")
            _, again = self.call("PATCH", path, {"status": "done"})
            self.assertEqual(again["completed_at"], "earlier")

        _, reopened = self.call("PATCH", path, {"status": "in_progress"})
        self.assertIsNone(reopened["completed_at"])
        _, cancelled = self.call("PATCH", path, {"status": "cancelled"})
        self.assertIsNone(cancelled["completed_at"])

        self.assertTrue(self.create(status="done")["completed_at"])
        status, _ = self.call("PATCH", path, {"completed_at": "2020-01-01T00:00:00Z"})
        self.assertEqual(status, 400)

    def test_blocked_status(self):
        ticket = self.create(title="stuck", status="blocked")
        self.assertEqual(self.titles("blocked"), ["stuck"])
        self.assertIsNone(ticket["completed_at"])

    def test_delete(self):
        ticket = self.create()
        path = f"/api/tickets/{ticket['id']}"
        self.assertEqual(self.call("DELETE", path)[0], 200)
        self.assertEqual(self.call("GET", path)[0], 404)
        self.assertEqual(self.call("DELETE", path)[0], 404)

    def test_invalid_input_is_rejected(self):
        ticket = self.create()
        path = f"/api/tickets/{ticket['id']}"
        cases = [
            ("POST", "/api/tickets", {}),
            ("POST", "/api/tickets", {"title": "   "}),
            ("POST", "/api/tickets", {"title": "x", "status": "doing"}),
            ("POST", "/api/tickets", {"title": "x", "priority": "urgent"}),
            ("POST", "/api/tickets", {"title": "x", "tags": ["a"]}),
            ("POST", "/api/tickets", ["not", "an", "object"]),
            ("PATCH", path, {}),
            ("PATCH", path, {"status": ["done"]}),
            ("PATCH", path, {"position": -1}),
            ("PATCH", path, {"position": True}),
            ("PATCH", path, {"project": 5}),
        ]
        for method, target, body in cases:
            with self.subTest(body=body):
                status, payload = self.call(method, target, body)
                self.assertEqual(status, 400, payload)
                self.assertIn("error", payload)
        self.assertEqual(self.call("POST", "/api/tickets", raw=b"{nope")[0], 400)
        self.assertEqual(self.call("GET", "/api/tickets?status=doing")[0], 400)
        self.assertEqual(self.call("GET", "/api/tickets?projct=web")[0], 400)
        self.assertEqual(self.call("GET", "/api/nope")[0], 404)

    def test_rejects_other_sites(self):
        created = {"title": "x"}
        evil = {"Origin": "https://evil.example"}
        self.assertEqual(self.call("POST", "/api/tickets", created, evil)[0], 403)
        self.assertEqual(self.call("GET", "/api/tickets", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/tickets")[1], [])
        own = {"Origin": self.base}
        self.assertEqual(self.call("POST", "/api/tickets", created, own)[0], 201)

    def test_concurrent_creates_keep_unique_positions(self):
        with ThreadPoolExecutor(8) as pool:
            statuses = list(
                pool.map(lambda i: self.call("POST", "/api/tickets", {"title": str(i)})[0], range(40))
            )
        self.assertEqual(set(statuses), {201})
        _, tickets = self.call("GET", "/api/tickets")
        self.assertEqual([t["position"] for t in tickets], list(range(40)))


class StartupTest(unittest.TestCase):
    def test_taken_port_leaves_database_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = make_server(Path(tmp) / "first.db", 0)
            second = Path(tmp) / "second.db"
            try:
                with self.assertRaises(OSError):
                    make_server(second, first.server_port)
            finally:
                first.server_close()
            self.assertFalse(second.exists())


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
