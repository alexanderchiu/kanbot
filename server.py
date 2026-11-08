#!/usr/bin/env python3
"""Kanbot, a local-only kanban board: JSON API for agents, web UI for humans. Stdlib only."""
import argparse
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STATUSES = ("follow_up", "in_progress", "blocked", "done", "cancelled")
PRIORITIES = ("high", "medium", "low")
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY = 1024 * 1024
BACKUPS_KEPT = 30
MAX_ID = 2**63 - 1
TICKET_PATH = re.compile(r"^/api/tickets/(\d{1,18})$")
CLAIM_PATH = re.compile(r"^/api/tickets/(\d{1,18})/claim$")
NOTES_PATH = re.compile(r"^/api/tickets/(\d{1,18})/notes$")

TICKETS_TABLE = """
CREATE TABLE {name} (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('follow_up', 'in_progress', 'blocked', 'done', 'cancelled')),
    priority    TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    project     TEXT,
    idea        INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    assignee    TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    completed_at TEXT
);
"""

NOTES_TABLE = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    author     TEXT,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL,
    actor     TEXT,
    field     TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    at        TEXT NOT NULL
);
"""
DEPENDENCIES_TABLE = """
CREATE TABLE IF NOT EXISTS dependencies (
    ticket_id  INTEGER NOT NULL,
    depends_on INTEGER NOT NULL,
    PRIMARY KEY (ticket_id, depends_on)
);
"""
RESOLVED_STATUSES = ("done", "cancelled")
HISTORY_FIELDS = ("title", "description", "status", "priority", "project", "idea", "assignee")

SELECT_TICKET = (
    "SELECT tickets.*,"
    " (SELECT COUNT(*) FROM notes WHERE notes.ticket_id = tickets.id) AS note_count"
    " FROM tickets"
)

API_DOCS = {
    "description": "Kanbot, a local kanban board. Request and response bodies are JSON.",
    "statuses": list(STATUSES),
    "priorities": list(PRIORITIES),
    "ticket_fields": {
        "title": "string, required on create",
        "description": "string, default ''",
        "status": "follow_up | in_progress | blocked | done | cancelled, default follow_up",
        "priority": "high | medium | low, default medium",
        "project": "string or null; project tag, stored lowercase",
        "idea": "boolean, default false; tags the ticket as an idea",
        "created_by": "string or null; who created the ticket, stored lowercase. "
        "Can only be set on create.",
        "assignee": "string or null; who is working on it, stored lowercase",
        "depends_on": "list of ticket IDs that must finish first. PATCH replaces the "
        "whole list. Circular dependencies are rejected.",
        "position": "integer >= 0; index within the column. If omitted, the ticket "
        "is placed by priority.",
    },
    "read_only_fields": {
        "id": "integer",
        "note_count": "integer",
        "blocked_by": "IDs from depends_on that aren't done or cancelled yet",
        "blocks": "IDs of tickets that depend on this one",
        "created_at": "UTC timestamp",
        "updated_at": "UTC timestamp",
        "completed_at": "UTC timestamp set when the ticket moves to done, "
        "cleared if it leaves done; otherwise null",
    },
    "endpoints": {
        "GET /api/tickets": "List tickets in board order. Optional query parameters: "
        "status, project, priority, idea (true or false), assignee, created_by, q "
        "(text search of title and description), ready=true (follow-up tickets with "
        "nothing left to wait for).",
        "POST /api/tickets": "Create a ticket. Returns it with status 201.",
        "GET /api/tickets/{id}": "Fetch one ticket, including its notes and its change "
        "history, newest change first.",
        "POST /api/tickets/{id}/notes": 'Add a note with {"body": "text", "author": '
        '"name"}. Notes are append-only, so use them for progress, handoffs and the '
        "reason a ticket is blocked or cancelled.",
        "PATCH /api/tickets/{id}": "Change any ticket fields. To move a ticket, send "
        '{"status": "done"}. To cancel one and keep it on record, send '
        '{"status": "cancelled"}. Add "actor": "name" to record who made the change.',
        "POST /api/tickets/{id}/claim": 'Send {"assignee": "name"} to take a ticket. '
        "Returns 409 if someone else already holds it. Prefer this to setting assignee "
        "with PATCH, which overrides the current holder.",
        "DELETE /api/tickets/{id}": "Delete a ticket permanently.",
        "GET /api/projects": "List project tags in use, with ticket counts.",
    },
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path):
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def transaction(db_path):
    """Serialise writes, so concurrent agents can't interleave column renumbering."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def tickets_table_outdated(conn):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tickets'").fetchone()
    if row is None:
        return False
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    return not (
        {"idea", "created_by", "assignee", "completed_at"} <= columns
        and all(f"'{status}'" in row["sql"] for status in STATUSES)
    )


def migrate(db_path):
    """Create the tickets table, or bring an older one up to date.

    SQLite can't change a CHECK constraint in place, so a table that predates the
    current schema is rebuilt. This runs in one transaction, so a failure leaves the
    old table untouched. Tickets already done get updated_at as their completion time,
    the closest record available.
    """
    with transaction(db_path) as conn:
        conn.execute(NOTES_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS notes_ticket ON notes (ticket_id)")
        conn.execute(DEPENDENCIES_TABLE)
        conn.execute(HISTORY_TABLE)
        conn.execute("CREATE INDEX IF NOT EXISTS history_ticket ON history (ticket_id)")
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tickets'").fetchone()
        if row is None:
            conn.execute(TICKETS_TABLE.format(name="tickets"))
            return
        if not tickets_table_outdated(conn):
            return

        columns = ", ".join(r["name"] for r in conn.execute("PRAGMA table_info(tickets)"))
        # Keep the ID counter, so IDs of deleted tickets are never reused.
        counter = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'tickets'"
        ).fetchone()
        conn.execute(TICKETS_TABLE.format(name="tickets_new"))
        conn.execute(f"INSERT INTO tickets_new ({columns}) SELECT {columns} FROM tickets")
        conn.execute("DROP TABLE tickets")
        conn.execute("ALTER TABLE tickets_new RENAME TO tickets")
        conn.execute(
            "UPDATE tickets SET completed_at = updated_at"
            " WHERE status = 'done' AND completed_at IS NULL"
        )
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'tickets'")
        if counter:
            conn.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('tickets', ?)", (counter["seq"],)
            )


def backup_database(db_path, backup_dir, keep=BACKUPS_KEPT, force=False):
    """Copy the database into backup_dir, unless it's unchanged since the last copy."""
    db = Path(db_path)
    if not db.exists():
        return None
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    pattern = f"{db.stem}-*.db"
    existing = sorted(backup_dir.glob(pattern))
    if not force and existing and existing[-1].stat().st_mtime >= db.stat().st_mtime:
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{db.stem}-{stamp}.db"
    source, copy = connect(db), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        source.close()
        copy.close()
    for old in sorted(backup_dir.glob(pattern))[:-keep]:
        old.unlink()
    return target


def normalize_name(value):
    return value.strip().lower() or None


def validate(body, creating):
    """Return the cleaned ticket fields from a request body, or raise ApiError."""
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    unknown = set(body) - set(API_DOCS["ticket_fields"])
    if unknown:
        raise ApiError(400, f"Unknown fields: {', '.join(sorted(unknown))}.")
    if creating and "title" not in body:
        raise ApiError(400, "title is required.")

    fields = {}
    if "title" in body:
        if not isinstance(body["title"], str) or not body["title"].strip():
            raise ApiError(400, "title must be a non-empty string.")
        fields["title"] = body["title"].strip()
    if "description" in body:
        if not isinstance(body["description"], str):
            raise ApiError(400, "description must be a string.")
        fields["description"] = body["description"]
    for name, allowed in (("status", STATUSES), ("priority", PRIORITIES)):
        if name in body:
            if body[name] not in allowed:
                raise ApiError(400, f"{name} must be one of: {', '.join(allowed)}.")
            fields[name] = body[name]
    if "created_by" in body and not creating:
        raise ApiError(400, "created_by can only be set when creating a ticket.")
    for name in ("project", "created_by", "assignee"):
        if name in body:
            value = body[name]
            if value is not None and not isinstance(value, str):
                raise ApiError(400, f"{name} must be a string or null.")
            fields[name] = normalize_name(value) if value else None
    if "idea" in body:
        if not isinstance(body["idea"], bool):
            raise ApiError(400, "idea must be true or false.")
        fields["idea"] = body["idea"]
    if "depends_on" in body:
        value = body["depends_on"]
        if not isinstance(value, list) or any(
            type(item) is not int or not 0 <= item <= MAX_ID for item in value
        ):
            raise ApiError(400, "depends_on must be a list of ticket IDs.")
        fields["depends_on"] = sorted(set(value))
    if "position" in body:
        value = body["position"]
        if type(value) is not int or value < 0:
            raise ApiError(400, "position must be an integer >= 0.")
        fields["position"] = value
    return fields


def place(conn, ticket_id, status, priority, position=None):
    """Put a ticket in its column and renumber that column.

    With no explicit position, the ticket goes before the first ticket of lower
    priority, so urgent work surfaces while manual ordering is respected.
    """
    others = conn.execute(
        "SELECT id, priority FROM tickets WHERE status = ? AND id != ? ORDER BY position, id",
        (status, ticket_id),
    ).fetchall()
    if position is None:
        rank = PRIORITIES.index(priority)
        index = next(
            (i for i, row in enumerate(others) if PRIORITIES.index(row["priority"]) > rank),
            len(others),
        )
    else:
        index = min(position, len(others))
    ordered = [row["id"] for row in others]
    ordered.insert(index, ticket_id)
    conn.executemany(
        "UPDATE tickets SET position = ? WHERE id = ?",
        [(i, tid) for i, tid in enumerate(ordered)],
    )


def ticket_json(row):
    return {**dict(row), "idea": bool(row["idea"])}


def attach_dependencies(conn, tickets):
    """Add depends_on, blocked_by and blocks to each ticket."""
    by_id = {ticket["id"]: ticket for ticket in tickets}
    for ticket in tickets:
        ticket.update(depends_on=[], blocked_by=[], blocks=[])
    rows = conn.execute(
        "SELECT d.ticket_id, d.depends_on, t.status FROM dependencies d"
        " JOIN tickets t ON t.id = d.depends_on ORDER BY d.depends_on, d.ticket_id"
    )
    for row in rows:
        if row["ticket_id"] in by_id:
            by_id[row["ticket_id"]]["depends_on"].append(row["depends_on"])
            if row["status"] not in RESOLVED_STATUSES:
                by_id[row["ticket_id"]]["blocked_by"].append(row["depends_on"])
        if row["depends_on"] in by_id:
            by_id[row["depends_on"]]["blocks"].append(row["ticket_id"])


def fetch_ticket(conn, ticket_id):
    row = conn.execute(f"{SELECT_TICKET} WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"Ticket {ticket_id} not found.")
    ticket = ticket_json(row)
    attach_dependencies(conn, [ticket])
    return ticket


def set_dependencies(conn, ticket_id, depends_on):
    """Replace a ticket's dependencies, refusing any that would form a cycle."""
    if ticket_id in depends_on:
        raise ApiError(400, "A ticket can't depend on itself.")
    for other in depends_on:
        if not conn.execute("SELECT 1 FROM tickets WHERE id = ?", (other,)).fetchone():
            raise ApiError(400, f"depends_on refers to ticket {other}, which doesn't exist.")

    edges = {}
    for row in conn.execute("SELECT ticket_id, depends_on FROM dependencies"):
        edges.setdefault(row["ticket_id"], []).append(row["depends_on"])
    for start in depends_on:
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            if current == ticket_id:
                raise ApiError(
                    400, f"Ticket {ticket_id} can't depend on ticket {start}: that would be circular."
                )
            if current not in seen:
                seen.add(current)
                stack.extend(edges.get(current, []))

    conn.execute("DELETE FROM dependencies WHERE ticket_id = ?", (ticket_id,))
    conn.executemany(
        "INSERT INTO dependencies (ticket_id, depends_on) VALUES (?, ?)",
        [(ticket_id, other) for other in depends_on],
    )


def dependency_text(ids):
    return ", ".join(f"#{other}" for other in ids) or None


def record_history(conn, ticket_id, actor, field, old, new, at):
    def text(value):
        return str(value).lower() if isinstance(value, bool) else value

    conn.execute(
        "INSERT INTO history (ticket_id, actor, field, old_value, new_value, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, actor, field, text(old), text(new), at),
    )


def create_ticket(db_path, body):
    fields = validate(body, creating=True)
    status = fields.get("status", "follow_up")
    priority = fields.get("priority", "medium")
    stamp = now()
    with transaction(db_path) as conn:
        ticket_id = conn.execute(
            "INSERT INTO tickets (title, description, status, priority, project, idea,"
            " created_by, assignee, created_at, updated_at, completed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fields["title"],
                fields.get("description", ""),
                status,
                priority,
                fields.get("project"),
                fields.get("idea", False),
                fields.get("created_by"),
                fields.get("assignee"),
                stamp,
                stamp,
                stamp if status == "done" else None,
            ),
        ).lastrowid
        place(conn, ticket_id, status, priority, fields.get("position"))
        set_dependencies(conn, ticket_id, fields.get("depends_on", []))
        record_history(conn, ticket_id, fields.get("created_by"), "created", None, None, stamp)
        return fetch_ticket(conn, ticket_id)


def update_ticket(db_path, ticket_id, body):
    actor = None
    if isinstance(body, dict) and "actor" in body:
        body = dict(body)
        actor = body.pop("actor")
        if actor is not None and not isinstance(actor, str):
            raise ApiError(400, "actor must be a string or null.")
        actor = normalize_name(actor) if actor else None
    fields = validate(body, creating=False)
    if not fields:
        raise ApiError(400, "No fields to update.")
    with transaction(db_path) as conn:
        current = fetch_ticket(conn, ticket_id)
        position = fields.pop("position", None)
        depends_on = fields.pop("depends_on", None)
        status = fields.get("status", current["status"])
        priority = fields.get("priority", current["priority"])
        moved = (
            position is not None
            or status != current["status"]
            or priority != current["priority"]
        )
        fields["updated_at"] = now()
        if status != current["status"]:
            fields["completed_at"] = fields["updated_at"] if status == "done" else None
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE tickets SET {assignments} WHERE id = ?",
            (*fields.values(), ticket_id),
        )
        if moved:
            place(conn, ticket_id, status, priority, position)
        for name in HISTORY_FIELDS:
            if name in fields and fields[name] != current[name]:
                record_history(
                    conn, ticket_id, actor, name, current[name], fields[name], fields["updated_at"]
                )
        if depends_on is not None and depends_on != current["depends_on"]:
            set_dependencies(conn, ticket_id, depends_on)
            record_history(
                conn,
                ticket_id,
                actor,
                "depends_on",
                dependency_text(current["depends_on"]),
                dependency_text(depends_on),
                fields["updated_at"],
            )
        return fetch_ticket(conn, ticket_id)


def claim_ticket(db_path, ticket_id, body):
    assignee = body.get("assignee") if isinstance(body, dict) else None
    if not isinstance(assignee, str) or not normalize_name(assignee):
        raise ApiError(400, "assignee must be a non-empty string.")
    assignee = normalize_name(assignee)
    with transaction(db_path) as conn:
        current = fetch_ticket(conn, ticket_id)
        if current["assignee"] not in (None, assignee):
            raise ApiError(409, f"Ticket {ticket_id} is already assigned to {current['assignee']}.")
        stamp = now()
        conn.execute(
            "UPDATE tickets SET assignee = ?, updated_at = ? WHERE id = ?",
            (assignee, stamp, ticket_id),
        )
        if current["assignee"] != assignee:
            record_history(conn, ticket_id, assignee, "assignee", current["assignee"], assignee, stamp)
        return fetch_ticket(conn, ticket_id)


def add_note(db_path, ticket_id, body):
    if not isinstance(body, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    unknown = set(body) - {"body", "author"}
    if unknown:
        raise ApiError(400, f"Unknown fields: {', '.join(sorted(unknown))}.")
    text, author = body.get("body"), body.get("author")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "body must be a non-empty string.")
    if author is not None and not isinstance(author, str):
        raise ApiError(400, "author must be a string or null.")
    stamp = now()
    with transaction(db_path) as conn:
        fetch_ticket(conn, ticket_id)
        note_id = conn.execute(
            "INSERT INTO notes (ticket_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (ticket_id, normalize_name(author) if author else None, text.strip(), stamp),
        ).lastrowid
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (stamp, ticket_id))
        return dict(conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone())


def delete_ticket(db_path, ticket_id):
    with transaction(db_path) as conn:
        fetch_ticket(conn, ticket_id)
        conn.execute("DELETE FROM notes WHERE ticket_id = ?", (ticket_id,))
        conn.execute("DELETE FROM history WHERE ticket_id = ?", (ticket_id,))
        conn.execute(
            "DELETE FROM dependencies WHERE ticket_id = ? OR depends_on = ?", (ticket_id, ticket_id)
        )
        conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    return {"deleted": ticket_id}


def list_tickets(db_path, query):
    params = {
        key: values[-1] for key, values in parse_qs(query, keep_blank_values=True).items()
    }
    unknown = set(params) - {"status", "project", "priority", "idea", "assignee", "created_by", "q", "ready"}
    if unknown:
        raise ApiError(400, f"Unknown query parameters: {', '.join(sorted(unknown))}.")

    where, args = [], []
    for name, allowed in (("status", STATUSES), ("priority", PRIORITIES)):
        if name in params:
            if params[name] not in allowed:
                raise ApiError(400, f"{name} must be one of: {', '.join(allowed)}.")
            where.append(f"{name} = ?")
            args.append(params[name])
    for name in ("project", "assignee", "created_by"):
        if name in params:
            where.append(f"{name} IS ?")
            args.append(normalize_name(params[name]))
    if "idea" in params:
        if params["idea"] not in ("true", "false"):
            raise ApiError(400, "idea must be true or false.")
        where.append("idea = ?")
        args.append(params["idea"] == "true")
    if "q" in params:
        escaped = re.sub(r"([\\%_])", r"\\\1", params["q"])
        where.append(r"(title LIKE ? ESCAPE '\' OR description LIKE ? ESCAPE '\')")
        args += [f"%{escaped}%"] * 2

    sql = SELECT_TICKET
    if where:
        sql += " WHERE " + " AND ".join(where)
    conn = connect(db_path)
    try:
        rows = [ticket_json(row) for row in conn.execute(sql, args)]
        attach_dependencies(conn, rows)
    finally:
        conn.close()
    if "ready" in params:
        if params["ready"] not in ("true", "false"):
            raise ApiError(400, "ready must be true or false.")
        wanted = params["ready"] == "true"
        rows = [t for t in rows if (t["status"] == "follow_up" and not t["blocked_by"]) == wanted]
    rows.sort(key=lambda t: (STATUSES.index(t["status"]), t["position"], t["id"]))
    return rows


def get_ticket(db_path, ticket_id):
    conn = connect(db_path)
    try:
        ticket = fetch_ticket(conn, ticket_id)
        notes = conn.execute(
            "SELECT * FROM notes WHERE ticket_id = ? ORDER BY id", (ticket_id,)
        ).fetchall()
        history = conn.execute(
            "SELECT actor, field, old_value, new_value, at FROM history"
            " WHERE ticket_id = ? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        return {
            **ticket,
            "notes": [dict(row) for row in notes],
            "history": [dict(row) for row in history],
        }
    finally:
        conn.close()


def list_projects(db_path):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT project AS name, COUNT(*) AS tickets FROM tickets"
            " WHERE project IS NOT NULL GROUP BY project ORDER BY project"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "kanbot"

    def log_request(self, code="-", size="-"):
        pass

    def send_json(self, status, payload):
        self.send_bytes(status, json.dumps(payload).encode(), "application/json")

    def send_bytes(self, status, data, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def check_request_source(self):
        """Stop other websites in the user's browser from reaching this server.

        The Host check defeats DNS rebinding; the Origin check defeats cross-site
        writes. Neither affects curl or other non-browser clients.
        """
        host = self.headers.get("Host", "")
        try:
            hostname = urlsplit(f"//{host}").hostname
            origin = self.headers.get("Origin")
            origin_host = urlsplit(origin).netloc if origin else host
        except ValueError:
            raise ApiError(403, "Invalid Host or Origin header.")
        allowed = self.server.allowed_hosts
        if ("*" not in allowed and hostname not in allowed) or origin_host != host:
            raise ApiError(
                403, f"Request refused: {hostname} isn't an allowed host. See KANBOT_ALLOWED_HOSTS."
            )

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ApiError(400, "Invalid Content-Length.")
        if length < 0:
            raise ApiError(400, "Invalid Content-Length.")
        if length > MAX_BODY:
            raise ApiError(413, "Request body too large.")
        try:
            body = json.loads(self.rfile.read(length))
            # JSON can carry lone surrogates, which SQLite can't store.
            json.dumps(body, ensure_ascii=False).encode()
            return body
        except ValueError:
            raise ApiError(400, "Request body must be valid JSON.")

    def route(self):
        self.check_request_source()
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        db = self.server.db_path
        method = self.command
        match = TICKET_PATH.match(path)
        claim = CLAIM_PATH.match(path)
        notes = NOTES_PATH.match(path)

        if method == "GET" and path == "/":
            html = (HERE / "index.html").read_bytes()
            return self.send_bytes(200, html, "text/html; charset=utf-8")
        if method == "GET" and path == "/api":
            return self.send_json(200, API_DOCS)
        if path == "/api/tickets":
            if method == "GET":
                return self.send_json(200, list_tickets(db, url.query))
            if method == "POST":
                return self.send_json(201, create_ticket(db, self.read_body()))
        if match:
            ticket_id = int(match[1])
            if method == "GET":
                return self.send_json(200, get_ticket(db, ticket_id))
            if method == "PATCH":
                return self.send_json(200, update_ticket(db, ticket_id, self.read_body()))
            if method == "DELETE":
                return self.send_json(200, delete_ticket(db, ticket_id))
        if claim and method == "POST":
            return self.send_json(200, claim_ticket(db, int(claim[1]), self.read_body()))
        if notes and method == "POST":
            return self.send_json(201, add_note(db, int(notes[1]), self.read_body()))
        if method == "GET" and path == "/api/projects":
            return self.send_json(200, list_projects(db))
        raise ApiError(404, f"No route for {method} {path}. See GET /api for the API docs.")

    def handle_request(self):
        try:
            self.route()
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            self.log_error("%s %s failed: %r", self.command, self.path, error)
            self.send_json(500, {"error": f"Internal error: {error}"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request


class Server(ThreadingHTTPServer):
    # The default queue of 5 drops connections when many agents call at once.
    request_queue_size = 128


def make_server(db_path, port, backup_dir=None, host="127.0.0.1", allowed_hosts=()):
    # Bind first: if the port is taken, another server is using the database.
    server = Server((host, port), Handler)
    extra = {name.strip().lower() for name in allowed_hosts if name.strip()}
    server.allowed_hosts = LOCAL_HOSTS | extra
    if backup_dir:
        # Back up before migrating, so a bad upgrade can be undone. A migration always
        # gets a fresh copy, because a restored file's old mtime can look unchanged.
        conn = connect(db_path)
        try:
            outdated = tickets_table_outdated(conn)
        finally:
            conn.close()
        backup_database(db_path, backup_dir, force=outdated)
    migrate(db_path)
    server.db_path = str(db_path)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    env = os.environ.get
    parser.add_argument(
        "--host",
        default=env("KANBOT_HOST", "127.0.0.1"),
        help="Address to listen on (env KANBOT_HOST). Use 0.0.0.0 in a container.",
    )
    parser.add_argument("--port", type=int, default=int(env("KANBOT_PORT", "8787")))
    parser.add_argument(
        "--db", default=env("KANBOT_DB", str(HERE / "kanban.db")), help="SQLite database path"
    )
    parser.add_argument(
        "--backup-dir",
        default=env("KANBOT_BACKUP_DIR"),
        help="Where to keep backups (default: backups/ beside the database)",
    )
    parser.add_argument(
        "--allowed-hosts",
        default=env("KANBOT_ALLOWED_HOSTS", ""),
        help="Comma-separated host names or IPs browsers may use to reach the board, besides "
        "localhost (env KANBOT_ALLOWED_HOSTS). * allows any, which removes DNS rebinding "
        "protection.",
    )
    args = parser.parse_args()
    backup_dir = args.backup_dir or Path(args.db).resolve().parent / "backups"
    server = make_server(args.db, args.port, backup_dir, args.host, args.allowed_hosts.split(","))
    print(f"Kanbot running on {args.host}:{server.server_port} (db: {args.db})", flush=True)
    if args.host not in LOCAL_HOSTS:
        print(f"Allowed hosts: {', '.join(sorted(server.allowed_hosts))}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
