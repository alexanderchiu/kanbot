# Changelog

All notable changes to Kanbot. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0, minor versions may include breaking changes to the API or data.

## Unreleased

## [0.2.0] - 2026-11-08

### Added

- Agent session tracking from Claude Code hooks, with a `kanbot-hooks` command to install them. Sessions report their state, model, ticket, working time, turns, tool calls and best-effort token counts. See [docs/agent-hooks.md](docs/agent-hooks.md).
- Agents view: live list of agent sessions by state, with a header badge for sessions waiting on you or a usage limit.
- `GET /api` and `kanbot --version` report the version.
- `deploy/deploy-synology.sh` has `--check`, `--dry-run`, `--ref` and `--port`, and waits for a health check after deploying.
- Release script and this changelog.
- Supabase and other Postgres databases, chosen with `--db` or `KANBOT_DATABASE_URL`. SQLite stays the default. See [docs/supabase.md](docs/supabase.md).
- Versioned Alembic migrations, applied at startup or with `kanbot migrate`. `--no-migrate` (`KANBOT_MIGRATE=0`) leaves migrations to CI and only checks the schema.
- `kanbot copy-db SOURCE TARGET` copies a whole board into a new, empty database, keeping IDs and timestamps.
- GitHub Actions workflow: lint and tests on SQLite and Postgres on every push, then Supabase migrations on `master`.
- Live updates: the server streams change events on `GET /api/events`, so the board, stats and agents views update straight away instead of polling every 3 seconds. With Postgres, servers sharing a database hear each other's changes through `LISTEN`/`NOTIFY`.
- Ticket descriptions follow a Why / What / How outline. New tickets in the UI start from it, and `GET /api` returns it as `description_template`.

### Changed

- The backend is now the `kanbot` package in `src/kanbot/`. It runs with `uv run kanbot`, is tested with pytest and is linted with ruff.
- The UI is split into CSS files and native ES modules, served under `/static/`, still with no build step.
- Fewer round trips to a remote database: no connection ping before each request, and reads that need several queries, such as opening a ticket, send them in one round trip on Postgres.
- The server now depends on SQLAlchemy, Alembic and psycopg. Existing SQLite files are upgraded and stamped with the first migration on start.
- The default database is `kanban.db` in the folder you start Kanbot from, rather than next to the code.

### Fixed

- Agent sessions no longer flicker, show phantom subagents, or vanish when a project filter is set.
- `docs/synology.md` wrongly said `curl` clients skip the host check.

## [0.1.0] - 2026-11-08

First version, built over two days of conversation with AI agents.

### Added

- Board with Follow-up, In progress, Blocked and Done columns, plus a hidden Cancelled column, with drag and drop.
- Tickets with priority, project, idea flag, creator, assignee, dependencies, notes and change history.
- JSON API for agents, including claims, a `ready` filter and circular-dependency checks.
- Stats view with completion times by creator, assignee, project and priority.
- Search, filters, keyboard shortcuts, live updates and a ticket side panel.
- Backups on start, a systemd user service with an install script, and Docker and Synology deployment.
