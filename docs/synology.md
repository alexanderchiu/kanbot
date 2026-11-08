# Running Kanbot on a Synology NAS

Kanbot runs as a small Docker container. The repo includes a `Dockerfile`, a `docker-compose.yml` and a script that deploys it, or redeploys it after changes, over SSH.

> [!WARNING]
> Kanbot has **no login**. Anyone who can reach the port can read, change and delete every ticket. Keep it on your home network: don't forward the port on your router or expose it through QuickConnect. If you need it remotely, use a VPN, such as Tailscale or Synology VPN Server, or a reverse proxy that adds authentication.

## What you need

- DSM 7.2 or later with **Container Manager** installed from Package Center.
- SSH enabled in **Control Panel › Terminal & SNMP**, for the script.
- An administrator account on the NAS.

## Option 1: deploy with the script (recommended)

From your computer, in this repo, start by checking the NAS is ready. This only reads, so it's safe to run any time:

```sh
deploy/deploy-synology.sh --check admin@nas.local
```

It checks SSH, Docker and Compose, that the folder is writable, your `.env` settings, whether a database exists, and which version is running. Fix anything marked `fail`.

Then deploy:

```sh
deploy/deploy-synology.sh --dry-run admin@nas.local        # show what it would do
deploy/deploy-synology.sh admin@nas.local                  # deploy what's committed
deploy/deploy-synology.sh --ref v0.1.0 admin@nas.local     # deploy a release
```

Options: `--dir` sets the folder on the NAS, which defaults to `/volume1/docker/kanbot`. Its parent shared folder must exist. `--port` must match `KANBOT_PUBLISH_PORT` if you changed it.

A deploy:

1. Copies the chosen commit with `git archive`, replacing the old code. Your local database, `.env` and uncommitted changes stay on your computer.
2. Creates `.env` from `.env.example` on the first run, then stops so you can edit it.
3. Builds the image, tagged with the version, and restarts the container with `docker compose up -d --build`. It asks for your NAS password because Docker needs `sudo` on DSM.
4. Waits for the container's health check and prints the running version. If the container doesn't become healthy, it shows the logs.

On the first run, edit `/volume1/docker/kanbot/.env` with File Station's text editor or over SSH. Set `KANBOT_ALLOWED_HOSTS` to every name and IP you'll use to open the board, then run the script again.

**To redeploy after changes**, commit them and run the same command. The NAS keeps its `.env` and `data/` folder.

## Option 2: Container Manager only

1. Copy the repo to `/volume1/docker/kanbot` with File Station. You only need `pyproject.toml`, `uv.lock`, `README.md`, the `src` folder, `Dockerfile`, `.dockerignore`, `docker-compose.yml` and `.env.example`.
2. Copy `.env.example` to `.env` in that folder and edit it.
3. In **Container Manager › Project › Create**, choose that folder, use the existing `docker-compose.yml`, and finish the wizard. It builds the image and starts the container.

**To update**, replace the files, then select the project and choose **Build**.

## Settings

Set these in `.env`:

| Setting | Meaning |
|---|---|
| `KANBOT_ALLOWED_HOSTS` | Comma-separated names or IPs you'll type in the browser, such as `nas.local,192.168.1.10`. `localhost` is always allowed. Browsers sending any other name get a 403, which blocks DNS rebinding attacks. `*` allows any name but removes that protection. |
| `KANBOT_PUBLISH_PORT` | Port on the NAS. The default is `8787`. |
| `KANBOT_DATABASE_URL` | Optional. A Supabase or other Postgres URL, to keep the data there instead of in `data/`. See [supabase.md](supabase.md). |
| `KANBOT_MIGRATE` | Optional. `0` stops the container changing the schema, for when CI runs migrations. |

The database and its backups live in `data/` next to `docker-compose.yml`. Include that folder in Hyper Backup.

## Moving your existing board to the NAS

1. Stop the local service so the database stops changing: `systemctl --user disable --now kanbot`.
2. Stop the container on the NAS: in Container Manager, or with `sudo docker compose stop` in its folder.
3. Copy your local `kanban.db` to `/volume1/docker/kanbot/data/kanban.db`.
4. Start the container again. It backs up and upgrades the database on start.

## Pointing agents at the NAS

Agents find the board through the URL in their instructions. Replace `http://127.0.0.1:8787` with your NAS's address, for example `http://nas.local:8787`, in `~/.claude/CLAUDE.md` and any project instructions. Add that name to `KANBOT_ALLOWED_HOSTS` too. Every client sends the name it used in the Host header, including `curl` and the hooks, and the board refuses names that aren't allowed.

## Troubleshooting

- **403 "isn't an allowed host"**: add the name in your browser's address bar to `KANBOT_ALLOWED_HOSTS`, then redeploy or restart the container.
- **Logs**: `sudo docker logs kanbot`, or the container's **Log** tab in Container Manager.
- **Health**: the container checks itself every 30 seconds. Container Manager shows it as healthy when the server responds.
