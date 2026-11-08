"""HTTP handler, routing, Host and Origin checks, and server setup."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from kanbot import __version__
from kanbot.agents import delete_agent, list_agents, record_event
from kanbot.api_docs import API_DOCS
from kanbot.db import Database
from kanbot.errors import ApiError
from kanbot.stats import ticket_stats
from kanbot.tickets import (
    add_note,
    claim_ticket,
    create_ticket,
    delete_ticket,
    get_ticket,
    list_projects,
    list_tickets,
    update_ticket,
)

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_BODY = 1024 * 1024
TICKET_PATH = re.compile(r"^/api/tickets/(\d{1,18})$")
CLAIM_PATH = re.compile(r"^/api/tickets/(\d{1,18})/claim$")
NOTES_PATH = re.compile(r"^/api/tickets/(\d{1,18})/notes$")
AGENT_PATH = re.compile(r"^/api/agents/([\w.:-]{1,200})$")
STATIC_PREFIX = "/static/"
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".html": "text/html; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json",
}
STATIC_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def read_static(url_path):
    """Return (bytes, content type) for a file under the package's static/ folder, or None.

    Only plain relative paths of allowed file types qualify: no empty or dot segments, no
    encoded separators, backslashes or NUL, however the request spells them.
    """
    name = unquote(url_path.removeprefix(STATIC_PREFIX))
    # One level of decoding only; a leftover % means a double-encoded path.
    if "%" in name or "\\" in name:
        return None
    segments = name.split("/")
    if not all(STATIC_SEGMENT.fullmatch(segment) for segment in segments):
        return None
    content_type = STATIC_TYPES.get("." + segments[-1].rsplit(".", 1)[-1].lower())
    if content_type is None:
        return None
    node = resources.files("kanbot").joinpath("static", *segments)
    if not node.is_file():
        return None
    return node.read_bytes(), content_type


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
            raise ApiError(403, "Invalid Host or Origin header.") from None
        allowed = self.server.allowed_hosts
        if ("*" not in allowed and hostname not in allowed) or origin_host != host:
            raise ApiError(
                403, f"Request refused: {hostname} isn't an allowed host. See KANBOT_ALLOWED_HOSTS."
            )

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ApiError(400, "Invalid Content-Length.") from None
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
            raise ApiError(400, "Request body must be valid JSON.") from None

    def route(self):
        self.check_request_source()
        url = urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        db = self.server.db
        method = self.command
        match = TICKET_PATH.match(path)
        claim = CLAIM_PATH.match(path)
        notes = NOTES_PATH.match(path)

        if method == "GET" and path == "/":
            html = (resources.files("kanbot") / "static" / "index.html").read_bytes()
            return self.send_bytes(200, html, "text/html; charset=utf-8")
        if method == "GET" and url.path.startswith(STATIC_PREFIX):
            found = read_static(url.path)
            if found is None:
                raise ApiError(404, f"No static file at {url.path}.")
            return self.send_bytes(200, *found)
        if method == "GET" and path == "/api":
            return self.send_json(200, {"version": __version__, **API_DOCS})
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
        if path == "/api/agent-events" and method == "POST":
            return self.send_json(200, record_event(db, self.read_body()))
        if path == "/api/agents" and method == "GET":
            include_ended = parse_qs(url.query).get("all") == ["true"]
            return self.send_json(200, list_agents(db, include_ended))
        if (agent := AGENT_PATH.match(path)) and method == "DELETE":
            return self.send_json(200, delete_agent(db, agent[1]))
        if method == "GET" and path == "/api/stats":
            return self.send_json(200, ticket_stats(db, url.query))
        raise ApiError(404, f"No route for {method} {path}. See GET /api for the API docs.")

    def handle_request(self):
        try:
            self.route()
        except ApiError as error:
            self.send_json(error.status, {"error": error.message})
        except BrokenPipeError, ConnectionResetError:
            pass
        except Exception as error:
            self.log_error("%s %s failed: %r", self.command, self.path, error)
            self.send_json(500, {"error": f"Internal error: {error}"})

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request


class Server(ThreadingHTTPServer):
    # The default queue of 5 drops connections when many agents call at once.
    request_queue_size = 128
    db = None

    def server_close(self):
        super().server_close()
        if self.db is not None:
            self.db.close()


def make_server(database, port, backup_dir=None, host="127.0.0.1", allowed_hosts=(), migrate=True):
    """Serve a database, given as a Database, a SQLite file path or a database URL.

    With migrate=False the schema is only checked, for setups where CI runs migrations.
    """
    # Bind first: if the port is taken, another server is using the database.
    server = Server((host, port), Handler)
    try:
        extra = {name.strip().lower() for name in allowed_hosts if name.strip()}
        server.allowed_hosts = LOCAL_HOSTS | extra
        server.db = database if isinstance(database, Database) else Database(database)
        if not migrate:
            if warning := server.db.check_schema():
                print(warning, flush=True)
            return server
        if backup_dir:
            # Back up before migrating, so a bad upgrade can be undone. A migration always
            # gets a fresh copy, because a restored file's old mtime can look unchanged.
            server.db.backup(backup_dir, force=server.db.needs_upgrade())
        server.db.upgrade()
    except BaseException:
        server.server_close()
        raise
    return server
