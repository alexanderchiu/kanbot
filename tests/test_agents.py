import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from backends import PostgresBackend, SqliteBackend

from kanbot import hook, hook_install
from kanbot.agents import ticket_from_command
from kanbot.web import make_server


class AgentsTest(SqliteBackend, unittest.TestCase):
    def setUp(self):
        self.db = self.new_database()
        self.server = make_server(self.db, 0)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.load(error)

    def event(self, name, session="s1", **fields):
        body = {"session_id": session, "hook_event_name": name, "cwd": "/work/Atlas", **fields}
        status, payload = self.call("POST", "/api/agent-events", body)
        self.assertEqual(status, 200, payload)

    def sessions(self, query=""):
        return self.call("GET", f"/api/agents{query}")[1]

    def backdate(self, column, seconds):
        for key, value in self.sql(f"SELECT key, {column} FROM agent_sessions"):
            if value:
                stamp = hook_time_minus(value, seconds)
                self.sql(
                    f"UPDATE agent_sessions SET {column} = :stamp WHERE key = :key",
                    {"stamp": stamp, "key": key},
                )

    def test_session_lifecycle(self):
        self.event("SessionStart", model="claude-opus-5-5")
        [session] = self.sessions()
        self.assertEqual((session["state"], session["model"]), ("idle", "claude-opus-5-5"))
        self.assertEqual(session["project"], "atlas")

        self.event("UserPromptSubmit", prompt="secret prompt text")
        self.event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
        [session] = self.sessions()
        self.assertEqual(session["state"], "working")
        self.assertEqual((session["turns"], session["tool_calls"]), (1, 1))
        self.assertEqual(session["last_tool"], "Bash")

        self.event("PermissionRequest", tool_name="Bash")
        [session] = self.sessions()
        self.assertEqual(session["state"], "needs_input")
        self.assertEqual(session["state_detail"], "Permission for Bash")
        self.event("PostToolUse", tool_name="Bash")
        self.assertEqual(self.sessions()[0]["state"], "working")

        self.event("Notification", notification_type="idle_prompt", message="Waiting")
        self.assertEqual(self.sessions()[0]["state"], "idle")
        self.event("Notification", notification_type="auth_success", message="ok")
        self.assertEqual(self.sessions()[0]["state"], "idle")

        self.event("StopFailure", error="rate_limit", last_assistant_message="Limit reached")
        [session] = self.sessions()
        self.assertEqual(session["state"], "rate_limited")
        self.assertEqual(session["state_detail"], "Limit reached")
        self.event("StopFailure", error="server_error")
        self.assertEqual(self.sessions()[0]["state"], "error")

        self.event("SessionEnd", reason="other")
        self.assertEqual(self.sessions(), [])
        [ended] = self.sessions("?all=true")
        self.assertEqual(ended["state"], "ended")
        self.assertTrue(ended["ended_at"])

    def test_prompts_and_tool_input_are_not_stored(self):
        self.event("UserPromptSubmit", prompt="top secret plan")
        self.event("PreToolUse", tool_name="Bash", tool_input={"command": "cat ~/.ssh/id_rsa"})
        dump = "\n".join(
            repr(self.sql(f"SELECT * FROM {table}"))
            for table in ("agent_sessions", "tickets", "notes", "history")
        )
        self.assertNotIn("top secret plan", dump)
        self.assertNotIn("id_rsa", dump)

    def test_working_time_accumulates_only_while_working(self):
        self.event("UserPromptSubmit")
        self.backdate("working_since", 120)
        self.assertGreaterEqual(self.sessions()[0]["working_seconds"], 120)
        self.event("Stop")
        self.backdate("state_since", 600)
        [session] = self.sessions()
        self.assertTrue(120 <= session["working_seconds"] < 130)
        self.assertGreaterEqual(session["state_seconds"], 600)

    def test_ticket_is_linked_from_claims_and_starts(self):
        status, ticket = self.call("POST", "/api/tickets", {"title": "Ship it"})
        self.event(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": f"curl -s {self.base}/api/tickets/{ticket['id']}/claim -d x"},
        )
        [session] = self.sessions()
        self.assertEqual(
            session["ticket"], {"id": ticket["id"], "title": "Ship it", "status": "follow_up"}
        )

    def test_ticket_from_command(self):
        url = "http://127.0.0.1:8787/api/tickets"
        self.assertEqual(ticket_from_command(f"curl {url}/42/claim -d '{{}}'"), 42)
        start = f"""curl -X PATCH {url}/7 -d '{{"status": "in_progress"}}'"""
        self.assertEqual(ticket_from_command(start), 7)
        self.assertIsNone(
            ticket_from_command(f"""curl -X PATCH {url}/7 -d '{{"status": "done"}}'""")
        )
        self.assertIsNone(ticket_from_command(f"curl {url}/7"))

    def test_usage_and_model_from_hook(self):
        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 100,
            "cache_creation_tokens": 20,
        }
        self.event("Stop", kanbot_usage=usage, kanbot_model="claude-sonnet-5-5")
        [session] = self.sessions()
        self.assertEqual(session["model"], "claude-sonnet-5-5")
        self.assertEqual({k: session[k] for k in usage}, usage)
        self.event("Stop", kanbot_usage={"input_tokens": -1, "output_tokens": "9"})
        self.assertEqual(self.sessions()[0]["input_tokens"], 10)

    def test_subagents_nest_under_their_session(self):
        self.event("UserPromptSubmit")
        self.event("SubagentStart", agent_id="a1", agent_type="Explore")
        self.event("PreToolUse", agent_id="a1", agent_type="Explore", tool_name="Read")
        [session] = self.sessions()
        [sub] = session["subagents"]
        self.assertEqual(
            (sub["agent_type"], sub["state"], sub["tool_calls"]), ("Explore", "working", 1)
        )
        self.assertEqual(session["tool_calls"], 0)
        self.event("SubagentStop", agent_id="a1", agent_type="Explore")
        self.assertEqual(self.sessions()[0]["subagents"][0]["state"], "ended")
        self.event("SubagentStart", agent_id="a2", agent_type="Plan")
        self.event("SessionEnd")
        [ended] = self.sessions("?all=true")
        self.assertEqual({s["state"] for s in ended["subagents"]}, {"ended"})

    def test_out_of_order_events_do_not_override_newer_state(self):
        self.event("UserPromptSubmit", kanbot_ts=100.0)
        self.event("Stop", kanbot_ts=105.0)
        # A tool event that fired before Stop but arrived after it.
        self.event("PostToolUse", tool_name="Bash", kanbot_ts=104.0)
        [session] = self.sessions()
        self.assertEqual(session["state"], "idle")
        self.event("PreToolUse", tool_name="Bash", kanbot_ts=103.0)
        [session] = self.sessions()
        self.assertEqual((session["state"], session["tool_calls"]), ("idle", 1))
        self.event("UserPromptSubmit", kanbot_ts=110.0)
        self.assertEqual(self.sessions()[0]["state"], "working")

    def test_internal_helper_agents_are_ignored(self):
        self.event("UserPromptSubmit")
        self.event("SubagentStart", agent_id="helper", agent_type="")
        self.event("SubagentStop", agent_id="helper")
        self.assertEqual(self.sessions("?all=true")[0]["subagents"], [])

    def test_stale_sessions_are_hidden_and_can_be_dismissed(self):
        self.event("UserPromptSubmit", session="old")
        self.backdate("last_event_at", 13 * 3600)
        self.assertEqual(self.sessions(), [])
        self.assertEqual(len(self.sessions("?all=true")), 1)
        self.assertEqual(self.call("DELETE", "/api/agents/old")[0], 200)
        self.assertEqual(self.sessions("?all=true"), [])
        self.assertEqual(self.call("DELETE", "/api/agents/old")[0], 404)

    def test_bad_events_are_rejected_and_unknown_ones_ignored(self):
        for body in (
            {},
            {"session_id": "s"},
            {"hook_event_name": "Stop"},
            [],
            {"session_id": 5, "hook_event_name": "Stop"},
        ):
            self.assertEqual(self.call("POST", "/api/agent-events", body)[0], 400)
        self.event("SomeFutureEvent")
        self.assertEqual(self.sessions()[0]["state"], "idle")


def hook_time_minus(value, seconds):
    from datetime import timedelta

    from kanbot.agents import parse_time

    return (parse_time(value) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class PostgresAgentsTest(PostgresBackend, AgentsTest):
    """The same tests against Postgres."""


class HookScriptTest(unittest.TestCase):
    def test_transcript_usage_counts_each_message_once(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5-5",
                    "usage": {
                        "input_tokens": 3,
                        "output_tokens": 7,
                        "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 9,
                    },
                },
            },
            # The same streamed message written again for its next content block.
            {
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 3, "output_tokens": 7},
                },
            },
            {
                "type": "assistant",
                "message": {
                    "id": "m2",
                    "model": "claude-opus-5-5",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            },
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
        totals, model = hook.transcript_usage(f.name)
        Path(f.name).unlink()
        self.assertEqual(
            totals,
            {
                "input_tokens": 4,
                "output_tokens": 9,
                "cache_read_tokens": 50,
                "cache_creation_tokens": 9,
            },
        )
        self.assertEqual(model, "claude-opus-5-5")

    def test_payload_carries_when_the_hook_fired(self):
        payload = hook.build_payload({"session_id": "s", "hook_event_name": "Stop"}, 123.5)
        self.assertEqual(payload["kanbot_ts"], 123.5)

    def test_payload_drops_prompts_and_tool_output(self):
        event = {
            "session_id": "s",
            "hook_event_name": "PostToolUse",
            "prompt": "secret",
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/passwd"},
            "tool_response": {"content": "root:x"},
            "transcript_path": "/nope",
        }
        payload = hook.build_payload(event)
        self.assertNotIn("prompt", payload)
        self.assertNotIn("tool_response", payload)
        self.assertNotIn("tool_input", payload)
        self.assertNotIn("transcript_path", payload)

    def test_payload_survives_a_missing_transcript(self):
        payload = hook.build_payload(
            {"session_id": "s", "hook_event_name": "Stop", "transcript_path": "/does/not/exist"}
        )
        self.assertNotIn("kanbot_usage", payload)

    def test_main_never_fails(self):
        with mock.patch("sys.stdin", io.StringIO("not json")):
            self.assertEqual(hook.main(), 0)
        with (
            mock.patch("sys.stdin", io.StringIO('{"session_id": "s", "hook_event_name": "Stop"}')),
            mock.patch.object(hook, "URL", "http://127.0.0.1:9/api/agent-events"),
        ):
            self.assertEqual(hook.main(), 0)


class HookInstallTest(unittest.TestCase):
    def run_cli(self, *args):
        with redirect_stdout(io.StringIO()) as out:
            hook_install.main([*args, "--settings", str(self.settings)])
        return out.getvalue()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Path(tmp.name) / "settings.json"
        mine = {"type": "command", "command": "notify-send done"}
        self.settings.write_text(
            json.dumps(
                {
                    "model": "opus",
                    "hooks": {"Stop": [{"hooks": [mine]}]},
                }
            )
        )

    def test_install_is_idempotent_and_keeps_other_settings(self):
        self.run_cli("install")
        self.run_cli("install")
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "opus")
        stop = [h for g in data["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(stop), 2)
        self.assertIn("notify-send done", [h["command"] for h in stop])
        ours = [h for h in stop if hook_install.MARKER in h["command"]]
        self.assertEqual(ours[0]["async"], True)
        self.assertEqual(len(data["hooks"]), len(hook_install.EVENTS))
        notification = data["hooks"]["Notification"][0]
        self.assertIn("permission_prompt", notification["matcher"])
        self.assertTrue(list(self.settings.parent.glob("settings.json.kanbot-backup-*")))
        self.assertIn("Installed for", self.run_cli("status"))

    def test_uninstall_restores_only_the_users_hooks(self):
        original = json.loads(self.settings.read_text())
        self.run_cli("install")
        self.run_cli("uninstall")
        self.assertEqual(json.loads(self.settings.read_text()), original)
        self.assertIn("Not installed", self.run_cli("status"))

    def test_install_creates_missing_settings(self):
        self.settings.unlink()
        self.run_cli("install")
        self.assertEqual(len(json.loads(self.settings.read_text())["hooks"]), 12)
