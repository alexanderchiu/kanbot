"""HTTP handler, routing, Host and Origin checks, and server setup."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from kanbot.api_docs import API_DOCS
from kanbot.db import backup_database, connect, migrate, tickets_table_outdated
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
            html = (resources.files("kanbot") / "static" / "index.html").read_bytes()
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
