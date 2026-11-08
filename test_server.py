import http.client
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

    def test_creator_and_assignee(self):
        ticket = self.create(created_by=" Claude ", assignee="Claude")
        self.assertEqual(ticket["created_by"], "claude")
        self.assertEqual(ticket["assignee"], "claude")
        self.assertIsNone(self.create(title="other")["created_by"])
        path = f"/api/tickets/{ticket['id']}"

        self.assertEqual(self.call("PATCH", path, {"created_by": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "x", "assignee": 1})[0], 400)
        self.assertEqual(self.call("PATCH", path, {"assignee": "Alex"})[1]["assignee"], "alex")
        self.assertIsNone(self.call("PATCH", path, {"assignee": None})[1]["assignee"])
        self.call("PATCH", path, {"assignee": "alex"})

        def titles(query):
            return [t["title"] for t in self.call("GET", f"/api/tickets?{query}")[1]]

        self.assertEqual(titles("assignee=ALEX"), ["Ticket"])
        self.assertEqual(titles("created_by=claude"), ["Ticket"])
        self.assertEqual(titles("assignee=nobody"), [])

    def test_claim(self):
        path = f"/api/tickets/{self.create()['id']}/claim"
        status, ticket = self.call("POST", path, {"assignee": "Agent-A"})
        self.assertEqual((status, ticket["assignee"]), (200, "agent-a"))
        self.assertEqual(self.call("POST", path, {"assignee": "agent-a"})[0], 200)
        status, error = self.call("POST", path, {"assignee": "agent-b"})
        self.assertEqual(status, 409)
        self.assertIn("agent-a", error["error"])
        for body in ({}, {"assignee": "  "}, {"assignee": 3}, []):
            self.assertEqual(self.call("POST", path, body)[0], 400)
        self.assertEqual(self.call("POST", "/api/tickets/999/claim", {"assignee": "x"})[0], 404)

    def test_only_one_concurrent_claim_wins(self):
        path = f"/api/tickets/{self.create()['id']}/claim"
        with ThreadPoolExecutor(8) as pool:
            statuses = list(
                pool.map(lambda i: self.call("POST", path, {"assignee": f"agent-{i}"})[0], range(8))
            )
        self.assertEqual(sorted(statuses), [200] + [409] * 7)

    def test_notes(self):
        ticket = self.create()
        self.assertEqual(ticket["note_count"], 0)
        path = f"/api/tickets/{ticket['id']}"
        status, note = self.call("POST", f"{path}/notes", {"body": " First ", "author": "Agent"})
        self.assertEqual(status, 201)
        self.assertEqual((note["body"], note["author"]), ("First", "agent"))
        self.call("POST", f"{path}/notes", {"body": "Second"})

        _, detail = self.call("GET", path)
        self.assertEqual([n["body"] for n in detail["notes"]], ["First", "Second"])
        self.assertIsNone(detail["notes"][1]["author"])
        self.assertEqual(detail["note_count"], 2)
        _, [listed] = self.call("GET", "/api/tickets")
        self.assertEqual(listed["note_count"], 2)
        self.assertNotIn("notes", listed)

        for body in ({}, {"body": "  "}, {"body": 1}, {"body": "x", "author": 2}, {"body": "x", "by": "y"}, []):
            self.assertEqual(self.call("POST", f"{path}/notes", body)[0], 400)
        self.assertEqual(self.call("POST", "/api/tickets/999/notes", {"body": "x"})[0], 404)

        self.call("DELETE", path)
        next_ticket = self.create()
        self.assertEqual(self.call("GET", f"/api/tickets/{next_ticket['id']}")[1]["notes"], [])
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notes").fetchone(), (0,))
        conn.close()

    def test_history(self):
        ticket = self.create(title="Start", created_by="Agent")
        path = f"/api/tickets/{ticket['id']}"

        def changes():
            history = self.call("GET", path)[1]["history"]
            return [(h["actor"], h["field"], h["old_value"], h["new_value"]) for h in reversed(history)]

        self.assertEqual(changes(), [("agent", "created", None, None)])

        self.call("PATCH", path, {"status": "done", "priority": "high", "idea": True, "actor": "Alex"})
        self.assertEqual(
            changes()[1:],
            [
                ("alex", "status", "follow_up", "done"),
                ("alex", "priority", "medium", "high"),
                ("alex", "idea", "false", "true"),
            ],
        )

        # Unchanged values and reordering aren't recorded.
        before = changes()
        self.call("PATCH", path, {"status": "done", "title": "Start", "position": 0})
        self.assertEqual(changes(), before)

        self.call("PATCH", path, {"title": "Renamed"})
        self.assertEqual(changes()[-1], (None, "title", "Start", "Renamed"))
        self.call("POST", f"{path}/claim", {"assignee": "bot"})
        self.assertEqual(changes()[-1], ("bot", "assignee", None, "bot"))

        self.assertEqual(self.call("PATCH", path, {"actor": "alex"})[0], 400)
        self.assertEqual(self.call("PATCH", path, {"title": "x", "actor": 3})[0], 400)
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "x", "actor": "a"})[0], 400)
        self.assertNotIn("history", self.call("GET", "/api/tickets")[1][0])

        self.call("DELETE", path)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM history").fetchone(), (0,))
        conn.close()

    def test_dependencies(self):
        first = self.create(title="first")["id"]
        second = self.create(title="second")["id"]
        third = self.create(title="third", depends_on=[second, first, first])
        self.assertEqual(third["depends_on"], [first, second])
        self.assertEqual(third["blocked_by"], [first, second])
        path = f"/api/tickets/{third['id']}"
        self.assertEqual(self.call("GET", f"/api/tickets/{first}")[1]["blocks"], [third["id"]])

        self.call("PATCH", f"/api/tickets/{first}", {"status": "done"})
        self.assertEqual(self.call("GET", path)[1]["blocked_by"], [second])
        self.call("PATCH", f"/api/tickets/{second}", {"status": "cancelled"})
        self.assertEqual(self.call("GET", path)[1]["blocked_by"], [])
        self.call("PATCH", f"/api/tickets/{second}", {"status": "follow_up"})

        _, updated = self.call("PATCH", path, {"depends_on": [first], "actor": "alex"})
        self.assertEqual(updated["depends_on"], [first])
        _, detail = self.call("GET", path)
        change = detail["history"][0]
        self.assertEqual(
            (change["actor"], change["field"], change["old_value"], change["new_value"]),
            ("alex", "depends_on", f"#{first}, #{second}", f"#{first}"),
        )
        self.assertEqual(self.call("PATCH", path, {"depends_on": []})[1]["depends_on"], [])

        for body in ({"depends_on": 1}, {"depends_on": ["1"]}, {"depends_on": [True]}, {"depends_on": [999]}):
            self.assertEqual(self.call("PATCH", path, body)[0], 400, body)
        self.assertEqual(self.call("PATCH", path, {"depends_on": [third["id"]]})[0], 400)
        self.assertEqual(self.call("POST", "/api/tickets", {"title": "x", "depends_on": [999]})[0], 400)
        self.assertEqual(len(self.call("GET", "/api/tickets")[1]), 3)

    def test_rejects_circular_dependencies(self):
        a = self.create(title="a")["id"]
        b = self.create(title="b", depends_on=[a])["id"]
        c = self.create(title="c", depends_on=[b])["id"]
        status, error = self.call("PATCH", f"/api/tickets/{a}", {"depends_on": [c]})
        self.assertEqual(status, 400)
        self.assertIn("circular", error["error"])
        self.assertEqual(self.call("GET", f"/api/tickets/{a}")[1]["depends_on"], [])
        # A shared dependency isn't a cycle.
        self.assertEqual(self.call("PATCH", f"/api/tickets/{c}", {"depends_on": [a, b]})[0], 200)

    def test_ready_filter(self):
        first = self.create(title="first")["id"]
        self.create(title="waiting", depends_on=[first])
        self.create(title="active", status="in_progress")

        def titles(query):
            return [t["title"] for t in self.call("GET", f"/api/tickets?{query}")[1]]

        self.assertEqual(titles("ready=true"), ["first"])
        self.call("PATCH", f"/api/tickets/{first}", {"status": "done"})
        self.assertEqual(titles("ready=true"), ["waiting"])
        self.assertEqual(self.call("GET", "/api/tickets?ready=maybe")[0], 400)

    def test_deleting_a_ticket_removes_its_dependencies(self):
        first = self.create(title="first")["id"]
        second = self.create(title="second", depends_on=[first])
        self.call("DELETE", f"/api/tickets/{first}")
        _, detail = self.call("GET", f"/api/tickets/{second['id']}")
        self.assertEqual((detail["depends_on"], detail["blocked_by"]), ([], []))

    def test_empty_filters(self):
        self.create(title="tagged", project="web", assignee="alex")
        self.create(title="untagged")

        def titles(query):
            return [t["title"] for t in self.call("GET", f"/api/tickets?{query}")[1]]

        self.assertEqual(titles("assignee="), ["untagged"])
        self.assertEqual(titles("project="), ["untagged"])
        self.assertEqual(len(titles("q=")), 2)
        self.assertEqual(self.call("GET", "/api/tickets?status=")[0], 400)

    def test_values_that_cannot_be_stored_are_rejected(self):
        for body in ({"title": "x", "depends_on": [2**70]}, {"title": "x", "depends_on": [-1]}):
            self.assertEqual(self.call("POST", "/api/tickets", body)[0], 400)
        raw = b'{"title": "\\ud800"}'
        self.assertEqual(self.call("POST", "/api/tickets", raw=raw)[0], 400)

    def test_negative_content_length_is_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        connection.putrequest("POST", "/api/tickets")
        connection.putheader("Content-Length", "-1")
        connection.endheaders()
        self.assertEqual(connection.getresponse().status, 400)
        connection.close()

    def test_unsupported_method_returns_json_error(self):
        status, payload = self.call("PUT", "/api/tickets", {"title": "x"})
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

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


class AllowedHostsTest(unittest.TestCase):
    def start(self, allowed):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = make_server(Path(tmp.name) / "t.db", 0, allowed_hosts=allowed)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def status(self, port, host, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        headers = {"Host": host, **({"Origin": origin} if origin else {})}
        connection.request("GET", "/api/tickets", headers=headers)
        status = connection.getresponse().status
        connection.close()
        return status

    def test_extra_hosts_are_allowed_case_insensitively(self):
        port = self.start(["NAS.local", " 192.168.1.10 ", ""])
        self.assertEqual(self.status(port, f"nas.local:{port}"), 200)
        self.assertEqual(self.status(port, f"192.168.1.10:{port}"), 200)
        self.assertEqual(self.status(port, f"localhost:{port}"), 200)
        self.assertEqual(self.status(port, f"evil.example:{port}"), 403)
        self.assertEqual(self.status(port, f"nas.local:{port}", "http://evil.example"), 403)

    def test_wildcard_allows_any_host_but_still_checks_origin(self):
        port = self.start(["*"])
        self.assertEqual(self.status(port, f"anything:{port}"), 200)
        self.assertEqual(self.status(port, f"anything:{port}", "http://evil.example"), 403)

    def test_default_is_local_only(self):
        port = self.start(())
        self.assertEqual(self.status(port, f"nas.local:{port}"), 403)


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
