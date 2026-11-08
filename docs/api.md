# API reference

Kanbot's JSON API. `GET /api` returns a machine-readable summary of the same information.

Request and response bodies are JSON. `curl -d` works as is, because the server doesn't check `Content-Type`.

## Tickets

| Field | Values | Default |
|---|---|---|
| `title` | Non-empty string (required on create) | |
| `description` | String, written as `Why:`, `What:` and `How:` lines (see below) | `""` |
| `status` | `follow_up`, `in_progress`, `blocked`, `done`, `cancelled` | `follow_up` |
| `priority` | `high`, `medium`, `low` | `medium` |
| `project` | String or `null`, stored lowercase | `null` |
| `idea` | `true` or `false`; tags the ticket as an idea | `false` |
| `created_by` | String or `null`, stored lowercase; can only be set on create | `null` |
| `assignee` | String or `null`, stored lowercase | `null` |
| `depends_on` | List of ticket IDs that must finish first; `PATCH` replaces the whole list | `[]` |
| `position` | Integer index within the column, from 0 | Placed by priority |

Tickets also return these read-only fields:

- `id`
- `note_count`
- `blocked_by`: IDs from `depends_on` that aren't `done` or `cancelled` yet.
- `blocks`: IDs of tickets that depend on this one.
- `created_at`: when the ticket was created, in UTC.
- `updated_at`: when the ticket last changed, in UTC.
- `completed_at`: when the ticket moved to `done`, in UTC. It's cleared if the ticket leaves `done`, and is `null` for every other status.

If you don't set `position`, a ticket goes before the first ticket of lower priority in its column, or at the end if there is none. Setting `position` or dragging a card overrides this.

### Descriptions

Write descriptions as three lines, so every ticket says why it exists:

```
Why: Descriptions vary, so the reason for a ticket gets lost.
What: New tickets start from a Why / What / How template.
How: Prefill the description in the UI and publish the template in GET /api.
```

`GET /api` returns this outline as `description_template`, and new tickets in the UI start from it. The server doesn't enforce it.

## API

| Request | Purpose |
|---|---|
| `GET /api` | API description, in JSON |
| `GET /api/tickets` | List tickets in board order; filter with `status`, `project`, `priority`, `idea`, `assignee`, `created_by`, `q`, `ready` |
| `POST /api/tickets` | Create a ticket |
| `GET /api/tickets/{id}` | Get a ticket, including its notes and change history |
| `PATCH /api/tickets/{id}` | Change or move a ticket; add `"actor": "name"` to record who did it |
| `POST /api/tickets/{id}/notes` | Add a note with `{"body": "text", "author": "name"}` |
| `POST /api/tickets/{id}/claim` | Take a ticket with `{"assignee": "name"}`; returns 409 if someone else holds it |
| `DELETE /api/tickets/{id}` | Delete a ticket permanently |
| `GET /api/projects` | List project tags with ticket counts |
| `GET /api/stats` | Ticket counts and average and median completion time, overall and by creator, assignee, project and priority; filter with `project` |
| `GET /api/events` | A stream of server-sent events, one after each change (see below) |

An empty `project` or `assignee` filter lists tickets without one, so `assignee=` finds unassigned work. Use `ready=true` to list follow-up tickets with nothing left to wait for. Circular dependencies are rejected, and a dependency doesn't move or lock a ticket: it only shows what the ticket is waiting on.

`GET /api/events` keeps the connection open and sends an event named `change` after each successful write, such as `data: {"kinds": ["tickets"]}`. The kinds are `tickets` and `agents`. A burst of changes arrives as one event. The event carries no ticket data, so refetch what you need. The UI uses it to update straight away. With Postgres, the server also hears about changes made through other servers sharing the database. Try it with `curl -N http://127.0.0.1:8787/api/events`.

Every change to a ticket's fields is recorded in its history, except reordering. History starts from when this feature was added, and deleting a ticket deletes its history.

`GET /api/stats` returns `total`, `open`, `by_status`, `avg_completion_seconds` and `median_completion_seconds`, then `by_creator`, `by_assignee`, `by_project` and `by_priority`. Each breakdown row has `name`, `tickets`, `open`, `done`, `cancelled`, `avg_completion_seconds` and `median_completion_seconds`, and rows are sorted by ticket count. A `name` of `null` collects tickets with no creator, assignee or project. Completion time runs from `created_at` to `completed_at` for done tickets; it is `null` when nothing has completed, and tickets with an unreadable or negative duration are left out of the time figures. Tickets moved to Done before completion tracking was added use their last-updated time as `completed_at`. The UI shows this under the Stats switch in the header (`#stats`).

API errors return a 4xx or 5xx status with `{"error": "message"}`.
