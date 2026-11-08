# Kanbot

Local kanban board: the `kanbot` package in `src/kanbot/` (API and SQLite storage) and its UI in `src/kanbot/static/`. See `docs/api.md` for the API.

Use `"project": "kanbot"` for this repo's tickets.

## Development

- Run: `uv run kanbot`
- Test: `uv run pytest`. Lint and format: `uv run ruff check` and `uv run ruff format`.
- The server has no third-party dependencies. Keep it that way unless asked.
- Frontend code is in `src/kanbot/static/`: `index.html` (markup only), `css/` and `js/` (native ES modules, one job each, shared state in `js/state.js`). There is no build step, bundler or npm package. Keep it that way unless asked; edit the files and reload.
- Commits are atomic and follow the template at `~/.config/git/message`.
- `kanban.db` holds real tickets from other projects. Test against a scratch database with `--db`.
- The data file and this folder keep the old name, `kanban`. Moving either means stopping the service first.
- After changing the Python code, or merging changes to it, run `./install.sh` to restart the service. Then check `curl -s http://127.0.0.1:8787/api`. The service doesn't restart itself, and the page and its CSS and JS are read fresh on every request, so the page can call endpoints the running server doesn't have yet.
- Push to `origin` (the user's private GitHub repo) after each batch of finished, tested commits.
