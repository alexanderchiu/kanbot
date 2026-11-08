#!/usr/bin/env bash
# Deploy or redeploy Kanbot to a Synology NAS over SSH, then rebuild and restart it.
#
#   deploy/deploy-synology.sh admin@nas.local [/volume1/docker/kanbot]
#
# Ships the committed code only (git archive), so local databases, .env and
# uncommitted changes never leave this machine. The NAS keeps its own .env and data/.
set -euo pipefail

target=${1:?usage: deploy/deploy-synology.sh user@nas [remote-dir]}
remote_dir=${2:-/volume1/docker/kanbot}
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

if [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
  echo "warning: uncommitted changes won't be deployed" >&2
fi

echo "Copying $(git -C "$repo" rev-parse --short HEAD) to $target:$remote_dir"
git -C "$repo" archive --format=tar HEAD | ssh "$target" "mkdir -p '$remote_dir/data' && tar -x -C '$remote_dir'"

# DSM puts docker in /usr/local/bin, which non-interactive SSH sessions don't have on PATH.
# It needs sudo, so allocate a terminal for the password prompt.
ssh -t "$target" "
  set -e
  export PATH=/usr/local/bin:\$PATH
  cd '$remote_dir'
  [ -f .env ] || { cp .env.example .env; echo 'Created .env from .env.example: edit KANBOT_ALLOWED_HOSTS, then rerun.'; }
  if sudo docker compose version >/dev/null 2>&1; then compose='sudo docker compose'; else compose='sudo docker-compose'; fi
  \$compose up -d --build
  \$compose ps
"
