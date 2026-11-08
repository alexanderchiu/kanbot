"""Ticket validation and storage: CRUD, dependencies, notes, claims, history, listing."""
import re
from urllib.parse import parse_qs

from kanbot.api_docs import API_DOCS
from kanbot.db import (
    PRIORITIES,
    RESOLVED_STATUSES,
    STATUSES,
    connect,
    now,
    transaction,
)
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
