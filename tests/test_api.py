import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kanbot.web import make_server


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
        _, edited = self.call("PATCH", path, {"title": "New", "priority": "high", "project": None})
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

    def set_times(self, ticket, created_at, completed_at, status="done"):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute(
                "UPDATE tickets SET status = ?, created_at = ?, completed_at = ? WHERE id = ?",
                (status, created_at, completed_at, ticket["id"]),
            )
        conn.close()

    def test_stats_empty_board(self):
        status, stats = self.call("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["open"], 0)
        self.assertEqual(sum(stats["by_status"].values()), 0)
        self.assertIsNone(stats["avg_completion_seconds"])
        self.assertIsNone(stats["median_completion_seconds"])
        for key in ("by_creator", "by_assignee", "by_project", "by_priority"):
            self.assertEqual(stats[key], [])

    def test_stats_counts_times_and_sorting(self):
        a = self.create(created_by="ann", project="web", priority="high")
        b = self.create(created_by="ann", project="web")
        c = self.create(created_by="bob", project="api", assignee="bob")
        self.create(created_by="ann", status="cancelled")
        self.create(created_by="ann", status="in_progress")
        self.set_times(a, "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
        self.set_times(b, "2026-01-01T00:00:00Z", "2026-01-01T03:00:00Z")
        self.set_times(c, "2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z")
        _, stats = self.call("GET", "/api/stats")
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["open"], 1)
        self.assertEqual(stats["by_status"]["done"], 3)
        self.assertEqual(stats["by_status"]["cancelled"], 1)
        self.assertEqual(stats["avg_completion_seconds"], round((3600 + 10800 + 600) / 3))
        self.assertEqual(stats["median_completion_seconds"], 3600)
        ann, bob = stats["by_creator"]
        self.assertEqual(
            ann,
            {
                "name": "ann",
                "tickets": 4,
                "open": 1,
                "done": 2,
                "cancelled": 1,
                "avg_completion_seconds": 7200,
                "median_completion_seconds": 7200,
            },
        )
        self.assertEqual(bob["name"], "bob")
        self.assertEqual(bob["median_completion_seconds"], 600)
        self.assertEqual([r["name"] for r in stats["by_priority"]], ["medium", "high"])

    def test_stats_null_names_get_their_own_row(self):
        self.create()
        self.create(created_by="ann", assignee="bob", project="web")
        _, stats = self.call("GET", "/api/stats")
        for key, name in (("by_creator", "ann"), ("by_assignee", "bob"), ("by_project", "web")):
            self.assertEqual({r["name"] for r in stats[key]}, {None, name})
            for row in stats[key]:
                self.assertEqual(row["tickets"], 1)
                self.assertIsNone(row["avg_completion_seconds"])

    def test_stats_ignores_bad_and_negative_durations(self):
        good = self.create()
        negative = self.create()
        garbage = self.create()
        missing = self.create()
        self.set_times(good, "2026-01-01T00:00:00Z", "2026-01-01T00:02:00Z")
        self.set_times(negative, "2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z")
        self.set_times(garbage, "not a date", "2026-01-01T00:00:00Z")
        self.set_times(missing, "2026-01-01T00:00:00Z", None)
        status, stats = self.call("GET", "/api/stats")
        self.assertEqual(status, 200)
        self.assertEqual(stats["by_status"]["done"], 4)
        self.assertEqual(stats["avg_completion_seconds"], 120)
        self.assertEqual(stats["median_completion_seconds"], 120)

    def test_stats_project_filter(self):
        self.create(project="web", created_by="ann")
        self.create(project="Web", created_by="bob")
        self.create(project="api")
        self.create()
        _, web = self.call("GET", "/api/stats?project=WEB")
        self.assertEqual(web["total"], 2)
        self.assertEqual([r["name"] for r in web["by_project"]], ["web"])
        _, none = self.call("GET", "/api/stats?project=")
        self.assertEqual(none["total"], 1)
        self.assertIsNone(none["by_project"][0]["name"])
        _, missing = self.call("GET", "/api/stats?project=nope")
        self.assertEqual(missing["total"], 0)

    def test_stats_rejects_unknown_parameters(self):
        status, body = self.call("GET", "/api/stats?status=done")
        self.assertEqual(status, 400)
        self.assertIn("status", body["error"])
        status, _ = self.call("POST", "/api/stats", {})
        self.assertEqual(status, 404)

    def test_projects_endpoint(self):
        self.create(project="web")
        self.create(project="Web")
        self.create(project="api")
        self.create()
        _, projects = self.call("GET", "/api/projects")
        self.assertEqual(projects, [{"name": "api", "tickets": 1}, {"name": "web", "tickets": 2}])

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

        for body in (
            {},
            {"body": "  "},
            {"body": 1},
            {"body": "x", "author": 2},
            {"body": "x", "by": "y"},
            [],
        ):
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
            return [
                (h["actor"], h["field"], h["old_value"], h["new_value"]) for h in reversed(history)
            ]

        self.assertEqual(changes(), [("agent", "created", None, None)])

        self.call(
            "PATCH", path, {"status": "done", "priority": "high", "idea": True, "actor": "Alex"}
        )
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

        for body in (
            {"depends_on": 1},
            {"depends_on": ["1"]},
            {"depends_on": [True]},
            {"depends_on": [999]},
        ):
            self.assertEqual(self.call("PATCH", path, body)[0], 400, body)
        self.assertEqual(self.call("PATCH", path, {"depends_on": [third["id"]]})[0], 400)
        self.assertEqual(
            self.call("POST", "/api/tickets", {"title": "x", "depends_on": [999]})[0], 400
        )
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
                pool.map(
                    lambda i: self.call("POST", "/api/tickets", {"title": str(i)})[0], range(40)
                )
            )
        self.assertEqual(set(statuses), {201})
        _, tickets = self.call("GET", "/api/tickets")
        self.assertEqual([t["position"] for t in tickets], list(range(40)))
