# Kanbot

A local kanban board with a web UI for you and a JSON API for your agents. It has no third-party dependencies, uses [uv](https://docs.astral.sh/uv/) to manage Python, and stores everything in `kanban.db` (SQLite) next to the server. The server listens on `127.0.0.1` only.

## Run

```sh
uv run server.py                  # http://127.0.0.1:8787
uv run server.py --port 9000 --db /path/to/board.db
```

## Run as a service

To run the board as a systemd user service that starts when you log in and restarts if it fails:

```sh
./install.sh
```

Run `./install.sh` again after updating the code. The service doesn't restart itself when files change, and `index.html` is served fresh on every request, so the page can get ahead of a server that hasn't restarted.

```sh
systemctl --user status kanbot
journalctl --user -u kanbot -f
```

The service stops when you log out, unless you run `loginctl enable-linger`. To remove it, run `systemctl --user disable --now kanbot`, then delete `~/.config/systemd/user/kanbot.service`.

## Backups

Each time the server starts, it copies the database to `backups/` beside it, unless nothing has changed since the last copy. It keeps the newest 30. To restore one, stop the server and copy a backup over `kanban.db`.

## Use the board

- Press `N` for a new ticket or `/` to search.
- Drag cards to move or reorder them. The board picks up agent changes within 3 seconds.
- Cards show the date a ticket was created, or completed once it's done. Open a card to see the exact times.
- Links in descriptions and notes are clickable.
- Click a card to edit, cancel or delete it. Cancelled tickets stay on record and are hidden until you turn on the **Cancelled** toggle. To reopen one, change its status.
- Use **New ticket** or the **+** button on a column to create a ticket.
- Use the search box to find tickets by number, text, project, assignee or creator.
- Use the **Project** menu to filter the board by project, or use the **Ideas** toggle to see just ideas.

## Tickets

| Field | Values | Default |
|---|---|---|
| `title` | Non-empty string (required on create) | |
| `description` | String | `""` |
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

An empty `project` or `assignee` filter lists tickets without one, so `assignee=` finds unassigned work. Use `ready=true` to list follow-up tickets with nothing left to wait for. Circular dependencies are rejected, and a dependency doesn't move or lock a ticket: it only shows what the ticket is waiting on.

Every change to a ticket's fields is recorded in its history, except reordering. History starts from when this feature was added, and deleting a ticket deletes its history.

API errors return a 4xx or 5xx status with `{"error": "message"}`.

## Instructions for agents

Paste this into your `CLAUDE.md` or your agent's instructions:

````markdown
## Task board

Track work on Kanbot, the local kanban board, at http://127.0.0.1:8787. Set `project` to the
current project's name. Run `curl -s http://127.0.0.1:8787/api/projects` to reuse an
existing tag.

- Before creating a ticket, check for a duplicate:
  `curl -s 'http://127.0.0.1:8787/api/tickets?q=login'`
- Starting work (priority is `high`, `medium` or `low`):
  `curl -s http://127.0.0.1:8787/api/tickets -d '{"title": "Fix login bug", "status": "in_progress", "priority": "high", "project": "website", "created_by": "claude-code", "assignee": "claude-code"}'`
- Taking an existing ticket, which fails with a 409 if someone else holds it:
  `curl -s http://127.0.0.1:8787/api/tickets/42/claim -d '{"assignee": "claude-code"}'`
- Recording work for later:
  `curl -s http://127.0.0.1:8787/api/tickets -d '{"title": "Add login tests", "description": "Cover the expired-session case.", "project": "website", "created_by": "claude-code"}'`
- Recording an idea, as opposed to committed work:
  `curl -s http://127.0.0.1:8787/api/tickets -d '{"title": "Try passkey login", "idea": true, "priority": "low", "project": "website", "created_by": "claude-code"}'`
- Finishing work:
  `curl -s -X PATCH http://127.0.0.1:8787/api/tickets/42 -d '{"status": "done", "actor": "claude-code"}'`

- Recording progress, a handoff, or why work is blocked or cancelled:
  `curl -s http://127.0.0.1:8787/api/tickets/42/notes -d '{"body": "Waiting on the API key.", "author": "claude-code"}'`
- Marking work as stuck:
  `curl -s -X PATCH http://127.0.0.1:8787/api/tickets/42 -d '{"status": "blocked", "actor": "claude-code"}'`
- Dropping work that's no longer needed, which keeps the ticket on record:
  `curl -s -X PATCH http://127.0.0.1:8787/api/tickets/42 -d '{"status": "cancelled", "actor": "claude-code"}'`

To find work to pick up, query `ready=true`. Add `"depends_on": [42]` when one ticket must wait for another. `GET /api` describes the full API.
````

## Test

```sh
uv run python -m unittest
```
