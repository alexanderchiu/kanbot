# Kanbot

Local kanban board: `server.py` (API and SQLite storage), `index.html` (UI). See `README.md` for the API.

Use `"project": "kanbot"` for this repo's tickets.

## Development

- Run: `uv run server.py`
- Test: `uv run python -m unittest`
- The server has no third-party dependencies. Keep it that way unless asked.
- Commits are atomic and follow the template at `~/.config/git/message`.
- `kanban.db` holds real tickets from other projects. Test against a scratch database with `--db`.
- The data file and this folder keep the old name, `kanban`. Moving either means stopping the service first.
- After changing `server.py`, or merging changes to it, run `./install.sh` to restart the service. Then check `curl -s http://127.0.0.1:8787/api`. The service doesn't restart itself, and `index.html` is served fresh, so the page can call endpoints the running server doesn't have yet.
- Push to `origin` (the user's private GitHub repo) after each batch of finished, tested commits.
