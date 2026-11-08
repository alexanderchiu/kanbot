"""Agent sessions: what each Claude Code session is doing, fed by its hooks.

Hooks post their event JSON to the board. Only derived figures are kept, such as the
state, model, counters and the ticket being worked on. Prompts, tool input and tool
output are never stored.
"""

import re
from datetime import UTC, datetime
from pathlib import PurePath

from kanbot.db import connect, now, transaction
from kanbot.errors import ApiError

# What a session is doing, from the user's point of view.
WORKING, NEEDS_INPUT, IDLE, RATE_LIMITED, ERROR, ENDED = (
    "working",
    "needs_input",
    "idle",
    "rate_limited",
    "error",
    "ended",
)
NEEDS_INPUT_NOTIFICATIONS = {
    "permission_prompt",
    "elicitation_dialog",
    "elicitation_url_dialog",
    "agent_needs_input",
}
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
# Sessions killed without a SessionEnd event would otherwise linger forever.
STALE_AFTER_SECONDS = 12 * 3600
DETAIL_LIMIT = 200

TICKET_URL = re.compile(r"/api/tickets/(\d{1,18})(/claim)?\b")
IN_PROGRESS = re.compile(r"""["']status["']\s*:\s*["']in_progress["']""")


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def seconds_between(start, end):
    return max(0.0, (parse_time(end) - parse_time(start)).total_seconds())


def ticket_from_command(command):
    """Find the ticket a shell command claims or starts, if any."""
    for match in TICKET_URL.finditer(command):
        if match[2] or IN_PROGRESS.search(command):
            return int(match[1])
    return None


def text(value, limit=DETAIL_LIMIT):
    return value[:limit] if isinstance(value, str) and value else None


def record_event(db_path, event):
    """Update a session from one hook event. Unknown events are accepted and ignored."""
    if not isinstance(event, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    session_id, name = event.get("session_id"), event.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id or len(session_id) > 200:
        raise ApiError(400, "session_id must be a non-empty string.")
    if not isinstance(name, str) or not name:
        raise ApiError(400, "hook_event_name must be a non-empty string.")

    agent_id = event.get("agent_id") if isinstance(event.get("agent_id"), str) else None
    if agent_id and not event.get("agent_type"):
        # Claude Code's internal helpers, such as prompt suggestions, report as subagents
        # with no type. They'd flash in and out of the list, so they're ignored.
        return {"ok": True, "ignored": "internal agent"}
    key = f"{session_id}:{agent_id}" if agent_id else session_id
    stamp = now()
    # When the hook fired. Hooks run in parallel and can arrive out of order, so a state
    # change older than the last one applied must not override it.
    fired = event.get("kanbot_ts")
    fired = float(fired) if type(fired) in (int, float) and fired > 0 else None
    with transaction(db_path) as conn:
        if agent_id:
            ensure_session(conn, session_id, session_id, None, event, stamp)
        session = ensure_session(conn, key, session_id, agent_id, event, stamp)
        changes = {"last_event_at": stamp}
        state, detail = None, None

        if name == "SessionStart":
            state = IDLE
            if isinstance(event.get("model"), str):
                changes["model"] = text(event["model"])
        elif name == "UserPromptSubmit":
            changes["turns"] = session["turns"] + 1
            state = WORKING
        elif name == "PreToolUse":
            changes["tool_calls"] = session["tool_calls"] + 1
            changes["last_tool"] = text(event.get("tool_name"), 80)
            state = WORKING
            tool_input = event.get("tool_input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if isinstance(command, str) and (ticket := ticket_from_command(command)):
                changes["ticket_id"] = ticket
        elif name in ("PostToolUse", "PostToolUseFailure", "SubagentStart"):
            state = WORKING
        elif name == "PermissionRequest":
            state, detail = NEEDS_INPUT, f"Permission for {text(event.get('tool_name'), 80)}"
        elif name == "Notification":
            kind = event.get("notification_type")
            if kind in NEEDS_INPUT_NOTIFICATIONS:
                state, detail = NEEDS_INPUT, text(event.get("message"))
            elif kind == "idle_prompt":
                state, detail = IDLE, text(event.get("message"))
        elif name == "Stop":
            state = IDLE
        elif name == "StopFailure":
            error = event.get("error")
            state = RATE_LIMITED if error == "rate_limit" else ERROR
            detail = text(event.get("last_assistant_message")) or text(error)
        elif name in ("SubagentStop", "SessionEnd"):
            state = ENDED
            changes["ended_at"] = stamp

        usage = event.get("kanbot_usage")
        if isinstance(usage, dict) and not agent_id:
            for field in USAGE_FIELDS:
                if type(usage.get(field)) is int and usage[field] >= 0:
                    changes[field] = usage[field]
        if isinstance(event.get("kanbot_model"), str) and not agent_id:
            changes["model"] = text(event["kanbot_model"])

        stale = (
            fired is not None and session["state_ts"] is not None and fired < session["state_ts"]
        )
        if state and not stale:
            changes.update(transition(session, state, stamp))
            changes["state_detail"] = detail
            if fired is not None:
                changes["state_ts"] = fired
        assignments = ", ".join(f"{column} = ?" for column in changes)
        conn.execute(
            f"UPDATE agent_sessions SET {assignments} WHERE key = ?", (*changes.values(), key)
        )
        if name == "SessionEnd":
            # Subagents can't outlive their session.
            conn.execute(
                "UPDATE agent_sessions SET state = ?, ended_at = ?, working_since = NULL"
                " WHERE parent_key = ? AND state != ?",
                (ENDED, stamp, session_id, ENDED),
            )
    return {"ok": True}


def ensure_session(conn, key, session_id, agent_id, event, stamp):
    row = conn.execute("SELECT * FROM agent_sessions WHERE key = ?", (key,)).fetchone()
    if row:
        return dict(row)
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    conn.execute(
        "INSERT INTO agent_sessions (key, session_id, agent_id, agent_type, parent_key, cwd,"
        " project, state, state_since, started_at, last_event_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            key,
            session_id,
            agent_id,
            text(event.get("agent_type"), 80) if agent_id else None,
            session_id if agent_id else None,
            text(cwd, 500),
            PurePath(cwd).name.lower() if cwd else None,
            IDLE,
            stamp,
            stamp,
            stamp,
        ),
    )
    return dict(conn.execute("SELECT * FROM agent_sessions WHERE key = ?", (key,)).fetchone())


def transition(session, state, stamp):
    """Changes for moving a session to a new state, keeping working time up to date."""
    changes = {}
    if state != session["state"]:
        changes.update(state=state, state_since=stamp)
    if state == WORKING and not session["working_since"]:
        changes["working_since"] = stamp
    elif state != WORKING and session["working_since"]:
        changes["working_seconds"] = session["working_seconds"] + seconds_between(
            session["working_since"], stamp
        )
        changes["working_since"] = None
    if session["state"] == ENDED and state != ENDED:
        changes["ended_at"] = None
    return changes


def session_json(row, stamp, tickets):
    session = dict(row)
    working = session.pop("working_seconds")
    if session.pop("working_since") and session["state"] == WORKING:
        working += seconds_between(row["working_since"], stamp)
    session["working_seconds"] = round(working)
    session["age_seconds"] = round(seconds_between(row["started_at"], stamp))
    session["state_seconds"] = round(seconds_between(row["state_since"], stamp))
    session["idle_seconds"] = round(seconds_between(row["last_event_at"], stamp))
    session["ticket"] = tickets.get(row["ticket_id"])
    session.pop("parent_key")
    return session


def list_agents(db_path, include_ended=False):
    stamp = now()
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM agent_sessions ORDER BY started_at DESC").fetchall()
        tickets = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id, title, status FROM tickets WHERE id IN"
                " (SELECT ticket_id FROM agent_sessions WHERE ticket_id IS NOT NULL)"
            )
        }
    finally:
        conn.close()

    def visible(row):
        if include_ended:
            return True
        return row["state"] != ENDED and seconds_between(row["last_event_at"], stamp) < (
            STALE_AFTER_SECONDS
        )

    sessions = [session_json(r, stamp, tickets) for r in rows if not r["parent_key"] and visible(r)]
    children = {}
    for row in rows:
        if row["parent_key"]:
            children.setdefault(row["parent_key"], []).append(session_json(row, stamp, tickets))
    for session in sessions:
        session["subagents"] = children.get(session["key"], [])
    return sessions


def delete_agent(db_path, session_id):
    with transaction(db_path) as conn:
        deleted = conn.execute(
            "DELETE FROM agent_sessions WHERE key = ? OR parent_key = ?", (session_id, session_id)
        ).rowcount
    if not deleted:
        raise ApiError(404, f"Session {session_id} not found.")
    return {"deleted": session_id}
