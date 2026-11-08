# Running Kanbot on Supabase

Kanbot stores its data in SQLite by default. It can also use Postgres, including a hosted [Supabase](https://supabase.com) project, so the board's data lives in the cloud rather than in a file on one machine. Kanbot itself still runs wherever you start it, such as the systemd service or the Synology container. Only the database moves.

Things to know first:

- **Your tickets leave your machine.** They're stored in your Supabase project, along with agent session details such as working folders and models. Prompts and tool output are never stored.
- **It's slower than a local file.** Each request makes a few round trips to Supabase, so pick a region near you.
- **Free projects pause.** Supabase pauses free projects after a week without activity, and the board can't connect until you restore the project in the dashboard.
- **Backups are Supabase's job.** Kanbot's automatic backups only cover SQLite files. Check what your Supabase plan includes, or take your own copies with `pg_dump`.

## 1. Create the project

1. Create a project at [supabase.com](https://supabase.com/dashboard), and save the database password.
2. Open **Connect** in the project dashboard, and copy the **Session pooler** connection string. It looks like:

   ```
   postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-eu-west-1.pooler.supabase.com:5432/postgres
   ```

   Use the session pooler, not the direct connection: the direct connection needs IPv6, which many home networks and GitHub's runners don't have. The transaction pooler (port 6543) also works.

3. Replace `[YOUR-PASSWORD]` with the database password. If the password has characters such as `@`, `:` or `/`, URL-encode them.

Treat this URL as a secret: it gives full access to the database.

## 2. Create the tables

Kanbot's schema is managed with versioned [Alembic](https://alembic.sqlalchemy.org) migrations, in `src/kanbot/migrations/versions/`. Apply them with:

```sh
KANBOT_DATABASE_URL='postgresql://...' uv run kanbot migrate
```

The server also applies pending migrations when it starts, unless it's started with `--no-migrate` (see [Migrations from CI](#migrations-from-ci)). It's safe to run `migrate` repeatedly, or from several places at once: a database lock makes concurrent runs take turns.

The migrations turn on row level security (RLS) for every table, with no policies. Supabase publishes tables in the `public` schema through its Data API, which anyone with your project's public anon key can call. RLS with no policies blocks that, so the tables can only be reached with the database password. Kanbot connects as the tables' owner, which RLS doesn't apply to.

## 3. Copy your existing board

Stop the board first, so tickets don't change mid-copy, then copy everything into the new database. Tickets, notes, dependencies, history and agent sessions keep their IDs and timestamps:

```sh
systemctl --user stop kanbot
uv run kanbot copy-db kanban.db 'postgresql://...'
```

The target must be empty, so a copy can't be mixed into an existing board by mistake. `kanban.db` isn't changed, apart from bringing its schema up to date, and stays as a backup.

## 4. Point Kanbot at Supabase

Set `KANBOT_DATABASE_URL`. `--db` also accepts a URL, but the environment keeps the password out of process listings.

- **systemd service:** create `~/.config/kanbot.env`, readable only by you (`chmod 600`):

  ```sh
  KANBOT_DATABASE_URL=postgresql://...
  ```

  The service reads this file if it exists. Run `./install.sh` to restart it with the new setting.
- **Docker or Synology:** add `KANBOT_DATABASE_URL=postgresql://...` to `.env` and redeploy. The `data/` folder is then unused.

Check it's connected with `curl -s http://127.0.0.1:8787/api`. The service log shows the database it's using, with the password hidden.

## Migrations from CI

`.github/workflows/ci.yml` runs the linters and tests on every push, including the Postgres tests, which start a throwaway Postgres container. On pushes to `master`, once the tests pass, it runs `kanbot migrate` against Supabase. To turn that on:

1. In the GitHub repository, open **Settings > Environments**, and create an environment called `supabase`.
2. Add a secret to it called `SUPABASE_DATABASE_URL`, holding the session pooler URL.
3. Optionally, add yourself as a required reviewer, so each migration waits for your approval.

Without the secret, the migrate job skips and says so. You can also start it by hand from the **Actions** tab, with **Run workflow** on `master`.

When CI owns migrations, start the server with `--no-migrate`, or set `KANBOT_MIGRATE=0`. The server then never changes the schema. It refuses to start if the database is behind its code, and starts with a warning if a newer version has already migrated it.

CI migrates as soon as code reaches `master`, which can be before your servers run the new code. Write migrations that the previous release can live with: add columns and tables first, and remove old ones in a later release, once nothing uses them.

## Writing a migration

1. Change the tables in `src/kanbot/schema.py`.
2. Draft a migration against a scratch database that's at the latest revision. Number it after the last one:

   ```sh
   uv run kanbot migrate --db scratch.db
   KANBOT_DATABASE_URL=scratch.db uv run alembic revision --autogenerate --rev-id 0002 -m "Add due dates"
   ```

3. Read and edit the file it writes in `src/kanbot/migrations/versions/`. Autogenerate misses some changes, such as renames and data updates. On SQLite, changes run in batch mode, which rebuilds the table.
4. Run `uv run pytest`. The tests apply every migration to SQLite and Postgres, and fail if the result doesn't match `schema.py`.

Never edit a migration that has already been applied to a database you care about. Write a new one instead.
