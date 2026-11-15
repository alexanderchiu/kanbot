import io
import json
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

from backends import PostgresBackend, SqliteBackend

from kanbot import hook
from kanbot.checkins import AUTHOR, Checkins, run_checkins, stamp_before
from kanbot.web import make_server


class CheckinsTest(SqliteBackend, unittest.TestCase):
    def setUp(self):
        self.db = self.new_database()
        self.server = make_server(self.db, 0)
        self.store = self.server.db
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        # The server's own job is off by default; tests drive one by hand.
        self.server.checkins = Checkins(self.server.db, self.server.changes, minutes=60)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def ticket(self, **fields):
        ticket = self.call("POST", "/api/tickets", {"title": "Work", **fields})
        self.quiet(ticket["id"])
        return ticket["id"]

    def quiet(self, ticket_id, minutes=61):
        """Make a ticket look untouched for a while."""
        self.sql(
            "UPDATE tickets SET updated_at = :stamp WHERE id = :id",
            {"stamp": stamp_before(minutes), "id": ticket_id},
        )
        self.sql(
            "UPDATE notes SET created_at = :stamp WHERE ticket_id = :id",
            {"stamp": stamp_before(minutes), "id": ticket_id},
        )

    def notes(self, ticket_id):
        return self.call("GET", f"/api/tickets/{ticket_id}")["notes"]

    def test_asks_about_quiet_assigned_work_only(self):
        working = self.ticket(status="in_progress", assignee="bot")
        blocked = self.ticket(status="blocked", assignee="bot")
        unassigned = self.ticket(status="blocked")
        self.ticket(status="follow_up", assignee="bot")
        self.ticket(status="done", assignee="bot")
        recent = self.call(
            "POST", "/api/tickets", {"title": "New", "status": "in_progress", "assignee": "bot"}
        )["id"]

        self.assertEqual(run_checkins(self.store), [working, blocked])
        [note] = self.notes(working)
        self.assertEqual(note["author"], AUTHOR)
        self.assertIn("@bot", note["body"])
        self.assertIn("still working", note["body"])
        self.assertIn("still blocked", self.notes(blocked)[0]["body"])
        self.assertEqual(self.notes(unassigned), [])
        self.assertEqual(self.notes(recent), [])

    def test_one_open_question_until_the_ticket_changes(self):
        ticket = self.ticket(status="in_progress", assignee="bot")
        self.assertEqual(run_checkins(self.store), [ticket])
        self.quiet(ticket)
        # Unanswered: no second question, however long it stays quiet.
        self.assertEqual(run_checkins(self.store), [])

        self.call("POST", f"/api/tickets/{ticket}/notes", {"body": "Still on it.", "author": "bot"})
        self.assertEqual(run_checkins(self.store), [])  # Answered just now.
        self.quiet(ticket)
        self.assertEqual(run_checkins(self.store), [ticket])
        self.assertEqual(len(self.notes(ticket)), 3)

    def test_agent_working_on_the_ticket_is_asked_once(self):
        ticket = self.ticket(status="in_progress", assignee="bot")
        claim = f"curl -s {self.base}/api/tickets/{ticket}/claim -d x"
        self.event("PreToolUse", tool_name="Bash", tool_input={"command": claim})
        self.quiet(ticket)
        run_checkins(self.store)

        reply = self.event("PostToolUse", tool_name="Bash")
        self.assertIn(f"#{ticket}", reply["message"])
        self.assertIn("still working", reply["message"])
        self.assertNotIn("message", self.event("PostToolUse", tool_name="Bash"))
        # Events Claude Code can't add context from, and other sessions' agents, get nothing.
        run_checkins(self.store)
        self.assertNotIn("message", self.event("UserPromptSubmit", session="s2"))

    def test_answered_question_is_not_passed_on(self):
        ticket = self.ticket(status="in_progress", assignee="bot")
        claim = f"curl -s {self.base}/api/tickets/{ticket}/claim -d x"
        self.event("PreToolUse", tool_name="Bash", tool_input={"command": claim})
        self.quiet(ticket)
        run_checkins(self.store)
        self.call("PATCH", f"/api/tickets/{ticket}", {"status": "done"})
        self.assertNotIn("message", self.event("UserPromptSubmit"))

    def event(self, name, session="s1", **fields):
        body = {"session_id": session, "hook_event_name": name, "cwd": "/work", **fields}
        return self.call("POST", "/api/agent-events", body)

    def test_job_runs_on_its_period_and_stops(self):
        ticket = self.ticket(status="in_progress", assignee="bot")
        job = Checkins(self.store, self.server.changes, minutes=60, first_run=0.05)
        job.start()
        self.addCleanup(job.stop)
        for _ in range(100):
            if self.notes(ticket):
                break
            threading.Event().wait(0.05)
        self.assertEqual(len(self.notes(ticket)), 1)
        job.stop()
        job.thread.join(2)
        self.assertFalse(job.thread.is_alive())

    def test_zero_minutes_turns_the_job_off(self):
        job = Checkins(self.store, self.server.changes, minutes=0)
        job.start()
        self.assertIsNone(job.thread)


class PostgresCheckinsTest(PostgresBackend, CheckinsTest):
    pass


class HookOutputTest(unittest.TestCase):
    def run_hook(self, event, reply):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(json.dumps(reply).encode())
        out = io.StringIO()
        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(event))),
            mock.patch("urllib.request.urlopen", return_value=response),
            redirect_stdout(out),
        ):
            self.assertEqual(hook.main(), 0)
        return out.getvalue()

    def test_message_becomes_additional_context(self):
        event = {"session_id": "s", "hook_event_name": "PostToolUse"}
        output = json.loads(self.run_hook(event, {"ok": True, "message": "Still busy?"}))
        self.assertEqual(
            output,
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "Still busy?",
                }
            },
        )

    def test_nothing_printed_without_a_message_or_for_other_events(self):
        self.assertEqual(
            self.run_hook(
                {"session_id": "s", "hook_event_name": "Stop"}, {"ok": True, "message": "Hi"}
            ),
            "",
        )
        self.assertEqual(
            self.run_hook({"session_id": "s", "hook_event_name": "PostToolUse"}, {"ok": True}), ""
        )
