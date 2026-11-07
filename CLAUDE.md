# Kanban

Local kanban board: `server.py` (API and SQLite storage), `index.html` (UI). See `README.md` for the API.

Use `"project": "kanban"` for this repo's tickets.

## Development

- Run: `uv run server.py`
- Test: `uv run python -m unittest`
- The server has no third-party dependencies. Keep it that way unless asked.
- Commits are atomic and follow the template at `~/.config/git/message`.
- `kanban.db` holds real tickets from other projects. Test against a scratch database with `--db`.
