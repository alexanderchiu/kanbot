"""The API description served at GET /api, also the list of ticket fields."""

from kanbot.db import PRIORITIES, STATUSES

# New tickets in the UI start from this; agents are asked to follow it too.
DESCRIPTION_TEMPLATE = "Why: \nWhat: \nHow: "

API_DOCS = {
    "description": "Kanbot, a local kanban board. Request and response bodies are JSON.",
    "statuses": list(STATUSES),
    "priorities": list(PRIORITIES),
    "description_template": DESCRIPTION_TEMPLATE,
    "ticket_fields": {
        "title": "string, required on create",
        "description": "string, default ''. Write it as three lines, Why: (the reason), "
        "What: (the deliverable) and How: (the approach), as in description_template.",
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
        "POST /api/agent-events": "Report a Claude Code hook event. Used by the hooks that "
        "kanbot-hooks installs; agents don't call it directly.",
        "GET /api/agents": "List active agent sessions with their state (working, "
        "needs_input, idle, rate_limited, error), model, ticket, working time, turns, "
        "tool calls and token counts. Add all=true to include ended and stale sessions.",
        "DELETE /api/agents/{session_id}": "Dismiss a session and its subagents.",
        "GET /api/stats": "Ticket statistics: totals, counts per status, and average and "
        "median completion time in seconds (created_at to completed_at, done tickets "
        "only), overall and broken down by_creator, by_assignee, by_project and "
        "by_priority. Rows are sorted by ticket count; name is null for tickets with no "
        "creator, assignee or project. Optional query parameter: project (empty means "
        "no project).",
    },
}
