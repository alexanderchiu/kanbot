"""Ticket validation and storage: CRUD, dependencies, notes, claims, history, listing."""

import re
from urllib.parse import parse_qs

from kanbot.api_docs import API_DOCS
from kanbot.db import PRIORITIES, RESOLVED_STATUSES, STATUSES, now
from kanbot.errors import ApiError

MAX_ID = 2**63 - 1
HISTORY_FIELDS = ("title", "description", "status", "priority", "project", "idea", "assignee")

SELECT_TICKET = (
    "SELECT tickets.*,"
    " (SELECT COUNT(*) FROM notes WHERE notes.ticket_id = tickets.id) AS note_count"
    " FROM tickets"
)


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
        "SELECT id, priority, position FROM tickets"
        " WHERE status = :status AND id != :id ORDER BY position, id",
        {"status": status, "id": ticket_id},
    ).fetchall()
    current = {row["id"]: row["position"] for row in others}
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
    # Only write rows that move, which saves round trips to a remote database.
    conn.executemany(
        "UPDATE tickets SET position = :position WHERE id = :id",
        [
            {"position": i, "id": tid}
            for i, tid in enumerate(ordered)
            if tid == ticket_id or current[tid] != i
        ],
    )


def ticket_json(row):
    return {**dict(row), "idea": bool(row["idea"])}


# Dependency rows with the status of the ticket depended on, for attach_dependencies.
SELECT_DEPENDENCIES = (
    "SELECT d.ticket_id, d.depends_on, t.status FROM dependencies d"
    " JOIN tickets t ON t.id = d.depends_on"
)
ORDER_DEPENDENCIES = " ORDER BY d.depends_on, d.ticket_id"


def ticket_queries(ticket_id):
    """The ticket and its dependency rows, as queries for Conn.fetch_many."""
    return (
        (f"{SELECT_TICKET} WHERE id = :id", {"id": ticket_id}),
        (
            f"{SELECT_DEPENDENCIES} WHERE d.ticket_id = :id OR d.depends_on = :id"
            + ORDER_DEPENDENCIES,
            {"id": ticket_id},
        ),
    )


def attach_dependencies(tickets, rows):
    """Add depends_on, blocked_by and blocks to each ticket, from dependency rows."""
    by_id = {ticket["id"]: ticket for ticket in tickets}
    for ticket in tickets:
        ticket.update(depends_on=[], blocked_by=[], blocks=[])
    for row in rows:
        if row["ticket_id"] in by_id:
            by_id[row["ticket_id"]]["depends_on"].append(row["depends_on"])
            if row["status"] not in RESOLVED_STATUSES:
                by_id[row["ticket_id"]]["blocked_by"].append(row["depends_on"])
        if row["depends_on"] in by_id:
            by_id[row["depends_on"]]["blocks"].append(row["ticket_id"])


def build_ticket(ticket_id, rows, dependency_rows):
    if not rows:
        raise ApiError(404, f"Ticket {ticket_id} not found.")
    ticket = ticket_json(rows[0])
    attach_dependencies([ticket], dependency_rows)
    return ticket


def fetch_ticket(conn, ticket_id):
    return build_ticket(ticket_id, *conn.fetch_many(*ticket_queries(ticket_id)))


def set_dependencies(conn, ticket_id, depends_on):
    """Replace a ticket's dependencies, refusing any that would form a cycle."""
    if ticket_id in depends_on:
        raise ApiError(400, "A ticket can't depend on itself.")
    ids, dependency_rows = conn.fetch_many(
        ("SELECT id FROM tickets", None), ("SELECT ticket_id, depends_on FROM dependencies", None)
    )
    existing = {row["id"] for row in ids}
    for other in depends_on:
        if other not in existing:
            raise ApiError(400, f"depends_on refers to ticket {other}, which doesn't exist.")

    edges = {}
    for row in dependency_rows:
        edges.setdefault(row["ticket_id"], []).append(row["depends_on"])
    for start in depends_on:
        seen, stack = set(), [start]
        while stack:
            current = stack.pop()
            if current == ticket_id:
                raise ApiError(
                    400,
                    f"Ticket {ticket_id} can't depend on ticket {start}: that would be circular.",
                )
            if current not in seen:
                seen.add(current)
                stack.extend(edges.get(current, []))

    conn.execute("DELETE FROM dependencies WHERE ticket_id = :id", {"id": ticket_id})
    conn.executemany(
        "INSERT INTO dependencies (ticket_id, depends_on) VALUES (:ticket_id, :depends_on)",
        [{"ticket_id": ticket_id, "depends_on": other} for other in depends_on],
    )


def dependency_text(ids):
    return ", ".join(f"#{other}" for other in ids) or None


def record_history(conn, ticket_id, actor, field, old, new, at):
    def text(value):
        return str(value).lower() if isinstance(value, bool) else value

    conn.execute(
        "INSERT INTO history (ticket_id, actor, field, old_value, new_value, at)"
        " VALUES (:ticket_id, :actor, :field, :old, :new, :at)",
        {
            "ticket_id": ticket_id,
            "actor": actor,
            "field": field,
            "old": text(old),
            "new": text(new),
            "at": at,
        },
    )


def create_ticket(db, body):
    fields = validate(body, creating=True)
    status = fields.get("status", "follow_up")
    priority = fields.get("priority", "medium")
    stamp = now()
    with db.transaction() as conn:
        ticket_id = conn.scalar(
            "INSERT INTO tickets (title, description, status, priority, project, idea,"
            " created_by, assignee, created_at, updated_at, completed_at)"
            " VALUES (:title, :description, :status, :priority, :project, :idea,"
            " :created_by, :assignee, :stamp, :stamp, :completed_at) RETURNING id",
            {
                "title": fields["title"],
                "description": fields.get("description", ""),
                "status": status,
                "priority": priority,
                "project": fields.get("project"),
                "idea": fields.get("idea", False),
                "created_by": fields.get("created_by"),
                "assignee": fields.get("assignee"),
                "stamp": stamp,
                "completed_at": stamp if status == "done" else None,
            },
        )
        place(conn, ticket_id, status, priority, fields.get("position"))
        set_dependencies(conn, ticket_id, fields.get("depends_on", []))
        record_history(conn, ticket_id, fields.get("created_by"), "created", None, None, stamp)
        return fetch_ticket(conn, ticket_id)


def update_ticket(db, ticket_id, body):
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
    with db.transaction() as conn:
        current = fetch_ticket(conn, ticket_id)
        position = fields.pop("position", None)
        depends_on = fields.pop("depends_on", None)
        status = fields.get("status", current["status"])
        priority = fields.get("priority", current["priority"])
        moved = (
            position is not None or status != current["status"] or priority != current["priority"]
        )
        fields["updated_at"] = now()
        if status != current["status"]:
            fields["completed_at"] = fields["updated_at"] if status == "done" else None
        # Column names come from validate(), never from the request.
        assignments = ", ".join(f"{name} = :{name}" for name in fields)
        conn.execute(
            f"UPDATE tickets SET {assignments} WHERE id = :ticket_id",
            {**fields, "ticket_id": ticket_id},
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


def claim_ticket(db, ticket_id, body):
    assignee = body.get("assignee") if isinstance(body, dict) else None
    if not isinstance(assignee, str) or not normalize_name(assignee):
        raise ApiError(400, "assignee must be a non-empty string.")
    assignee = normalize_name(assignee)
    with db.transaction() as conn:
        current = fetch_ticket(conn, ticket_id)
        if current["assignee"] not in (None, assignee):
            raise ApiError(409, f"Ticket {ticket_id} is already assigned to {current['assignee']}.")
        stamp = now()
        conn.execute(
            "UPDATE tickets SET assignee = :assignee, updated_at = :stamp WHERE id = :id",
            {"assignee": assignee, "stamp": stamp, "id": ticket_id},
        )
        if current["assignee"] != assignee:
            record_history(
                conn, ticket_id, assignee, "assignee", current["assignee"], assignee, stamp
            )
        return fetch_ticket(conn, ticket_id)


def add_note(db, ticket_id, body):
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
    with db.transaction() as conn:
        fetch_ticket(conn, ticket_id)
        note = conn.execute(
            "INSERT INTO notes (ticket_id, author, body, created_at)"
            " VALUES (:ticket_id, :author, :body, :stamp)"
            " RETURNING id, ticket_id, author, body, created_at",
            {
                "ticket_id": ticket_id,
                "author": normalize_name(author) if author else None,
                "body": text.strip(),
                "stamp": stamp,
            },
        ).one()
        conn.execute(
            "UPDATE tickets SET updated_at = :stamp WHERE id = :id",
            {"stamp": stamp, "id": ticket_id},
        )
        return dict(note)


def delete_ticket(db, ticket_id):
    with db.transaction() as conn:
        fetch_ticket(conn, ticket_id)
        ids = {"id": ticket_id}
        conn.execute("DELETE FROM notes WHERE ticket_id = :id", ids)
        conn.execute("DELETE FROM history WHERE ticket_id = :id", ids)
        conn.execute("DELETE FROM dependencies WHERE ticket_id = :id OR depends_on = :id", ids)
        conn.execute("DELETE FROM tickets WHERE id = :id", ids)
    return {"deleted": ticket_id}


def list_tickets(db, query):
    params = {key: values[-1] for key, values in parse_qs(query, keep_blank_values=True).items()}
    unknown = set(params) - {
        "status",
        "project",
        "priority",
        "idea",
        "assignee",
        "created_by",
        "q",
        "ready",
    }
    if unknown:
        raise ApiError(400, f"Unknown query parameters: {', '.join(sorted(unknown))}.")

    where, args = [], {}
    for name, allowed in (("status", STATUSES), ("priority", PRIORITIES)):
        if name in params:
            if params[name] not in allowed:
                raise ApiError(400, f"{name} must be one of: {', '.join(allowed)}.")
            where.append(f"{name} = :{name}")
            args[name] = params[name]
    for name in ("project", "assignee", "created_by"):
        if name in params:
            where.append(f"{name} IS NOT DISTINCT FROM :{name}")
            args[name] = normalize_name(params[name])
    if "idea" in params:
        if params["idea"] not in ("true", "false"):
            raise ApiError(400, "idea must be true or false.")
        where.append("idea = :idea")
        args["idea"] = params["idea"] == "true"
    if "q" in params:
        escaped = re.sub(r"([\\%_])", r"\\\1", params["q"])
        # lower() on both sides: LIKE ignores case in SQLite but not in Postgres.
        like = r"LIKE lower(:q) ESCAPE '\'"
        where.append(f"(lower(title) {like} OR lower(description) {like})")
        args["q"] = f"%{escaped}%"

    sql = SELECT_TICKET
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db.connect() as conn:
        ticket_rows, dependency_rows = conn.fetch_many(
            (sql, args), (SELECT_DEPENDENCIES + ORDER_DEPENDENCIES, None)
        )
    rows = [ticket_json(row) for row in ticket_rows]
    attach_dependencies(rows, dependency_rows)
    if "ready" in params:
        if params["ready"] not in ("true", "false"):
            raise ApiError(400, "ready must be true or false.")
        wanted = params["ready"] == "true"
        rows = [t for t in rows if (t["status"] == "follow_up" and not t["blocked_by"]) == wanted]
    rows.sort(key=lambda t: (STATUSES.index(t["status"]), t["position"], t["id"]))
    return rows


def get_ticket(db, ticket_id):
    ids = {"id": ticket_id}
    with db.connect() as conn:
        rows, dependency_rows, notes, history = conn.fetch_many(
            *ticket_queries(ticket_id),
            ("SELECT * FROM notes WHERE ticket_id = :id ORDER BY id", ids),
            (
                "SELECT actor, field, old_value, new_value, at FROM history"
                " WHERE ticket_id = :id ORDER BY id DESC",
                ids,
            ),
        )
    ticket = build_ticket(ticket_id, rows, dependency_rows)
    return {
        **ticket,
        "notes": [dict(row) for row in notes],
        "history": [dict(row) for row in history],
    }


def list_projects(db):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT project AS name, COUNT(*) AS tickets FROM tickets"
            " WHERE project IS NOT NULL GROUP BY project ORDER BY project"
        ).fetchall()
    return [dict(row) for row in rows]
