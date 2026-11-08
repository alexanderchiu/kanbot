"""Ticket statistics for GET /api/stats."""

import statistics
from datetime import datetime
from urllib.parse import parse_qs

from kanbot.db import RESOLVED_STATUSES, STATUSES, connect
from kanbot.errors import ApiError
from kanbot.tickets import normalize_name


def completion_seconds(created_at, completed_at):
    """Seconds from creation to completion, or None if either is missing, odd or negative."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        start = datetime.strptime(created_at, fmt)
        end = datetime.strptime(completed_at, fmt)
    except TypeError, ValueError:
        return None
    seconds = (end - start).total_seconds()
    return seconds if seconds >= 0 else None


def summarize(rows):
    """Counts and completion times for rows of (status, created_at, completed_at)."""
    times = []
    for status, created_at, completed_at in rows:
        if status == "done":
            seconds = completion_seconds(created_at, completed_at)
            if seconds is not None:
                times.append(seconds)
    return {
        "tickets": len(rows),
        "open": sum(1 for row in rows if row[0] not in RESOLVED_STATUSES),
        "done": sum(1 for row in rows if row[0] == "done"),
        "cancelled": sum(1 for row in rows if row[0] == "cancelled"),
        "avg_completion_seconds": round(statistics.fmean(times)) if times else None,
        "median_completion_seconds": round(statistics.median(times)) if times else None,
    }


def ticket_stats(db_path, query):
    params = {key: values[-1] for key, values in parse_qs(query, keep_blank_values=True).items()}
    unknown = set(params) - {"project"}
    if unknown:
        raise ApiError(400, f"Unknown query parameters: {', '.join(sorted(unknown))}.")
    sql = (
        "SELECT status, priority, project, created_by, assignee, created_at, completed_at"
        " FROM tickets"
    )
    args = []
    if "project" in params:
        sql += " WHERE project IS ?"
        args.append(normalize_name(params["project"]))
    conn = connect(db_path)
    try:
        tickets = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    def triple(t):
        return (t["status"], t["created_at"], t["completed_at"])

    def breakdown(column):
        groups = {}
        for t in tickets:
            groups.setdefault(t[column], []).append(triple(t))
        rows = [{"name": name, **summarize(group)} for name, group in groups.items()]
        rows.sort(key=lambda r: (-r["tickets"], r["name"] is None, r["name"] or ""))
        return rows

    overall = summarize([triple(t) for t in tickets])
    by_status = {status: 0 for status in STATUSES}
    for t in tickets:
        by_status[t["status"]] += 1
    return {
        "total": overall["tickets"],
        "open": overall["open"],
        "by_status": by_status,
        "avg_completion_seconds": overall["avg_completion_seconds"],
        "median_completion_seconds": overall["median_completion_seconds"],
        "by_creator": breakdown("created_by"),
        "by_assignee": breakdown("assignee"),
        "by_project": breakdown("project"),
        "by_priority": breakdown("priority"),
    }
