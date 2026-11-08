# Kanbot

Local kanban board: the `kanbot` package in `src/kanbot/` (API and SQLite storage) and its UI in `src/kanbot/static/`. See `docs/api.md` for the API.

Use `"project": "kanbot"` for this repo's tickets.

## Development

- Run: `uv run kanbot`
- Test: `uv run pytest`. Lint and format: `uv run ruff check` and `uv run ruff format`.
- The server's dependencies are SQLAlchemy, Alembic and psycopg. Don't add more unless asked. `src/kanbot/hook.py` must stay standard-library only.
- Data goes in SQLite (default) or Postgres/Supabase (`KANBOT_DATABASE_URL`). Queries are plain SQL with `:named` parameters through `Database.connect()` (reads) and `Database.transaction()` (writes, serialised). Write SQL that works on both.
- Schema changes are new Alembic migrations in `src/kanbot/migrations/versions/` plus the matching change to `src/kanbot/schema.py`; never edit an applied migration or `legacy.py`. See `docs/supabase.md`.
- Tests run on SQLite and on Postgres through testcontainers (Docker). `KANBOT_TEST_POSTGRES=0` skips Postgres.
- Frontend code is in `src/kanbot/static/`: `index.html` (markup only), `css/` and `js/` (native ES modules, one job each, shared state in `js/state.js`). There is no build step, bundler or npm package. Keep it that way unless asked; edit the files and reload.
- Commits are atomic and follow the template at `~/.config/git/message`.
- `kanban.db` holds real tickets from other projects. Test against a scratch database with `--db`.
- The data file and this folder keep the old name, `kanban`. Moving either means stopping the service first.
- After changing the Python code, or merging changes to it, run `./install.sh` to restart the service. Then check `curl -s http://127.0.0.1:8787/api`. The service doesn't restart itself, and the page and its CSS and JS are read fresh on every request, so the page can call endpoints the running server doesn't have yet.
- Push to `origin` (the user's private GitHub repo) after each batch of finished, tested commits.
