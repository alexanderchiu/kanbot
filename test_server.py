import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server import make_server


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.tmp.name) / "test.db", 0)
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


if __name__ == "__main__":
    unittest.main()
