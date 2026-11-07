#!/usr/bin/env python3
"""Local-only kanban board: JSON API for agents, web UI for humans. Stdlib only."""
import argparse
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
STATUSES = ("follow_up", "in_progress", "done")
PRIORITIES = ("high", "medium", "low")
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY = 1024 * 1024
TICKET_PATH = re.compile(r"^/api/tickets/(\d{1,18})$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL CHECK (status IN ('follow_up', 'in_progress', 'done')),
    priority    TEXT NOT NULL CHECK (priority IN ('high', 'medium', 'low')),
    project     TEXT,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

API_DOCS = {
    "description": "Local kanban board. Request and response bodies are JSON.",
    "statuses": list(STATUSES),
    "priorities": list(PRIORITIES),
    "ticket_fields": {
        "title": "string, required on create",
        "description": "string, default ''",
        "status": "follow_up | in_progress | done, default follow_up",
        "priority": "high | medium | low, default medium",
        "project": "string or null; project tag, stored lowercase",
        "idea": "boolean, default false; tags the ticket as an idea",
        "position": "integer >= 0; index within the column. If omitted, the ticket "
        "is placed by priority.",
    },
    "endpoints": {
        "GET /api/tickets": "List tickets in board order. Optional query parameters: "
        "status, project, priority, idea (true or false), q (text search of title "
        "and description).",
        "POST /api/tickets": "Create a ticket. Returns it with status 201.",
        "GET /api/tickets/{id}": "Fetch one ticket.",
        "PATCH /api/tickets/{id}": "Change any ticket fields. To move a ticket, send "
        '{"status": "done"}.',
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


def normalize_project(value):
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
    if "project" in body:
        value = body["project"]
        if value is not None and not isinstance(value, str):
            raise ApiError(400, "project must be a string or null.")
        fields["project"] = normalize_project(value) if value else None
    if "idea" in body:
        if not isinstance(body["idea"], bool):
            raise ApiError(400, "idea must be true or false.")
        fields["idea"] = body["idea"]
    if "position" in body:
        value = body["position"]
        if type(value) is not int or value < 0:
            raise ApiError(400, "position must be an integer >= 0.")
        fields["position"] = value
    return fields


def place(conn, ticket_id, status, priority, position=None):
    """Put a ticket in its column and renumber that column.

    With no explicit position, the ticket goes after the last ticket of equal or
    higher priority, so urgent work surfaces while manual ordering is respected.
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


def fetch_ticket(conn, ticket_id):
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise ApiError(404, f"Ticket {ticket_id} not found.")
    return ticket_json(row)


def create_ticket(db_path, body):
    fields = validate(body, creating=True)
    status = fields.get("status", "follow_up")
    priority = fields.get("priority", "medium")
    stamp = now()
    with transaction(db_path) as conn:
        ticket_id = conn.execute(
            "INSERT INTO tickets (title, description, status, priority, project, idea,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fields["title"],
                fields.get("description", ""),
                status,
                priority,
                fields.get("project"),
                fields.get("idea", False),
                stamp,
                stamp,
            ),
        ).lastrowid
        place(conn, ticket_id, status, priority, fields.get("position"))
        return fetch_ticket(conn, ticket_id)


def update_ticket(db_path, ticket_id, body):
    fields = validate(body, creating=False)
    if not fields:
        raise ApiError(400, "No fields to update.")
    with transaction(db_path) as conn:
        current = fetch_ticket(conn, ticket_id)
        position = fields.pop("position", None)
        status = fields.get("status", current["status"])
        priority = fields.get("priority", current["priority"])
        moved = (
            position is not None
            or status != current["status"]
            or priority != current["priority"]
        )
        fields["updated_at"] = now()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        conn.execute(
            f"UPDATE tickets SET {assignments} WHERE id = ?",
            (*fields.values(), ticket_id),
        )
        if moved:
            place(conn, ticket_id, status, priority, position)
        return fetch_ticket(conn, ticket_id)


def delete_ticket(db_path, ticket_id):
    with transaction(db_path) as conn:
        fetch_ticket(conn, ticket_id)
        conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    return {"deleted": ticket_id}


def list_tickets(db_path, query):
    params = {key: values[-1] for key, values in parse_qs(query).items()}
    unknown = set(params) - {"status", "project", "priority", "idea", "q"}
    if unknown:
        raise ApiError(400, f"Unknown query parameters: {', '.join(sorted(unknown))}.")

    where, args = [], []
    for name, allowed in (("status", STATUSES), ("priority", PRIORITIES)):
        if name in params:
            if params[name] not in allowed:
                raise ApiError(400, f"{name} must be one of: {', '.join(allowed)}.")
            where.append(f"{name} = ?")
            args.append(params[name])
    if "project" in params:
        where.append("project IS ?")
        args.append(normalize_project(params["project"]))
    if "idea" in params:
        if params["idea"] not in ("true", "false"):
            raise ApiError(400, "idea must be true or false.")
        where.append("idea = ?")
        args.append(params["idea"] == "true")
    if "q" in params:
        escaped = re.sub(r"([\\%_])", r"\\\1", params["q"])
        where.append(r"(title LIKE ? ESCAPE '\' OR description LIKE ? ESCAPE '\')")
        args += [f"%{escaped}%"] * 2

    sql = "SELECT * FROM tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    conn = connect(db_path)
    try:
        rows = [ticket_json(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()
    rows.sort(key=lambda t: (STATUSES.index(t["status"]), t["position"], t["id"]))
    return rows


def get_ticket(db_path, ticket_id):
    conn = connect(db_path)
    try:
        return fetch_ticket(conn, ticket_id)
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
    server_version = "kanban"

    def log_message(self, format, *args):
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
        if hostname not in ALLOWED_HOSTS or origin_host != host:
            raise ApiError(403, "Request refused: this server is local-only.")

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ApiError(400, "Invalid Content-Length.")
        if length > MAX_BODY:
            raise ApiError(413, "Request body too large.")
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            raise ApiError(400, "Request body must be valid JSON.")

    def route(self):
        self.check_request_source()
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        db = self.server.db_path
        method = self.command
        match = TICKET_PATH.match(path)

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

    do_GET = do_POST = do_PATCH = do_DELETE = handle_request


def make_server(db_path, port):
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    # Databases created before the idea tag existed need the column added.
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(tickets)")]
    if "idea" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN idea INTEGER NOT NULL DEFAULT 0")
    conn.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db_path = str(db_path)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=str(HERE / "kanban.db"), help="SQLite database path")
    args = parser.parse_args()
    server = make_server(args.db, args.port)
    print(f"Kanban running at http://127.0.0.1:{server.server_port} (db: {args.db})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
