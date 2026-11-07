# Kanban

A local kanban board with a web UI for you and a JSON API for your agents. It has no third-party dependencies, uses [uv](https://docs.astral.sh/uv/) to manage Python, and stores everything in `kanban.db` (SQLite) next to the server. The server listens on `127.0.0.1` only.

## Run

```sh
uv run server.py                  # http://127.0.0.1:8787
uv run server.py --port 9000 --db /path/to/board.db
```

## Backups

Each time the server starts, it copies the database to `backups/` beside it, unless nothing has changed since the last copy. It keeps the newest 30. To restore one, stop the server and copy a backup over `kanban.db`.

## Use the board

- Drag cards to move or reorder them. The board picks up agent changes within 3 seconds.
- Cards show the date a ticket was created, or completed once it's done. Open a card to see the exact times.
- Click a card to edit, cancel or delete it. Cancelled tickets stay on record and are hidden until you tick **Show cancelled**. To reopen one, change its status.
- Use **+ Add** on a column to create a ticket.
- Use the **Project** menu to filter the board by project, or tick **Ideas only** to see just ideas.

## Tickets

| Field | Values | Default |
|---|---|---|
| `title` | Non-empty string (required on create) | |
| `description` | String | `""` |
| `status` | `follow_up`, `in_progress`, `done`, `cancelled` | `follow_up` |
| `priority` | `high`, `medium`, `low` | `medium` |
| `project` | String or `null`, stored lowercase | `null` |
| `idea` | `true` or `false`; tags the ticket as an idea | `false` |
| `position` | Integer index within the column, from 0 | Placed by priority |

Tickets also return these read-only fields:

- `id`
- `created_at`: when the ticket was created, in UTC.
- `updated_at`: when the ticket last changed, in UTC.
- `completed_at`: when the ticket moved to `done`, in UTC. It's cleared if the ticket leaves `done`, and is `null` for every other status.

If you don't set `position`, a ticket goes after the last ticket of equal or higher priority in its column. Setting `position` or dragging a card overrides this.

## API

| Request | Purpose |
|---|---|
| `GET /api` | API description, in JSON |
| `GET /api/tickets` | List tickets in board order; filter with `status`, `project`, `priority`, `idea`, `q` |
| `POST /api/tickets` | Create a ticket |
| `GET /api/tickets/{id}` | Get a ticket |
| `PATCH /api/tickets/{id}` | Change or move a ticket |
| `DELETE /api/tickets/{id}` | Delete a ticket permanently |
| `GET /api/projects` | List project tags with ticket counts |

Errors return a 4xx or 5xx status with `{"error": "message"}`.

## Instructions for agents

Paste this into your `CLAUDE.md` or your agent's instructions:

````markdown
## Task board

Track work on the local kanban board at http://127.0.0.1:8787. Set `project` to the
current project's name. Run `curl -s http://127.0.0.1:8787/api/projects` to reuse an
existing tag.

- Before creating a ticket, check for a duplicate:
  `curl -s 'http://127.0.0.1:8787/api/tickets?q=login'`
- Starting work (priority is `high`, `medium` or `low`):
  `curl -s http://127.0.0.1:8787/api/tickets -d '{"title": "Fix login bug", "status": "in_progress", "priority": "high", "project": "website"}'`
- Recording work for later:
  `curl -s http://127.0.0.1:8787/api/tickets -d '{"title": "Add login tests", "description": "Cover the expired-session case.", "project": "website"}'`
- Recording an idea, as opposed to committed work:
  `curl -s http://127.0.0.1:8787/api/tickets -d '{"title": "Try passkey login", "idea": true, "priority": "low", "project": "website"}'`
- Finishing work:
  `curl -s -X PATCH http://127.0.0.1:8787/api/tickets/42 -d '{"status": "done"}'`

- Dropping work that's no longer needed, which keeps the ticket on record:
  `curl -s -X PATCH http://127.0.0.1:8787/api/tickets/42 -d '{"status": "cancelled"}'`

To see open work, query `status=follow_up` and `status=in_progress`. `GET /api` describes the full API.
````

## Test

```sh
uv run python -m unittest
```
