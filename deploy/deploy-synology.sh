#!/usr/bin/env bash
# Deploy Kanbot to a Synology NAS over SSH: copy the code, rebuild the image, restart the
# container and wait until it's healthy.
#
#   deploy/deploy-synology.sh [options] user@nas
#
#   --check      Check the NAS is ready, without changing anything. Safe to run any time.
#   --dry-run    Show what a deploy would do, without changing anything.
#   --ref REF    Deploy a tag, branch or commit, such as v0.1.0. Default: HEAD.
#   --dir DIR    Folder on the NAS. Default: /volume1/docker/kanbot.
#   --port PORT  Port the container publishes on the NAS. Default: 8787.
#
# Only committed code is shipped (git archive), so local databases, .env and uncommitted
# changes never leave this machine. The NAS keeps its own .env and data/ folder.
set -euo pipefail

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

mode=deploy ref=HEAD remote_dir=/volume1/docker/kanbot port=8787 target=
while [ $# -gt 0 ]; do
  case $1 in
    --check) mode=check ;;
    --dry-run) mode=dry-run ;;
    --ref) ref=${2:?--ref needs a value}; shift ;;
    --dir) remote_dir=${2:?--dir needs a value}; shift ;;
    --port) port=${2:?--port needs a value}; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage 1 ;;
    *) target=$1 ;;
  esac
  shift
done
[ -n "$target" ] || usage 1

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [ -t 1 ]; then green=$'\033[32m' yellow=$'\033[33m' red=$'\033[31m' reset=$'\033[0m'
else green= yellow= red= reset=; fi
ok() { printf '  %sok%s    %s\n' "$green" "$reset" "$*"; }
warn() { printf '  %swarn%s  %s\n' "$yellow" "$reset" "$*"; }
fail() { printf '  %sfail%s  %s\n' "$red" "$reset" "$*"; failed=1; }
failed=0

# DSM keeps docker in /usr/local/bin, which non-interactive SSH sessions lack on PATH.
remote() { ssh -o ConnectTimeout=10 "$target" "export PATH=/usr/local/bin:/usr/bin:/bin:\$PATH; $*"; }

echo "Local"
if commit=$(git -C "$repo" rev-parse --short "$ref^{commit}" 2>/dev/null); then
  version=$(git -C "$repo" show "$ref:pyproject.toml" | sed -n 's/^version = "\(.*\)"/\1/p')
  ok "$ref is $commit, version ${version:-unknown}"
else
  fail "$ref isn't a commit in this repo"
fi
if [ "$ref" = HEAD ] && [ -n "$(git -C "$repo" status --porcelain --untracked-files=no)" ]; then
  warn "uncommitted changes won't be deployed"
fi

echo "NAS ($target)"
if ! remote true 2>/dev/null; then
  fail "can't connect over SSH. Is SSH enabled in Control Panel > Terminal & SNMP?"
  exit 1
fi
ok "SSH connects"

# One round trip gathers everything the checks need, without sudo or changes.
facts=$(remote "
  command -v docker >/dev/null && echo docker=yes
  docker compose version >/dev/null 2>&1 && echo compose=plugin
  command -v docker-compose >/dev/null && echo compose=standalone
  command -v tar >/dev/null && echo tar=yes
  parent=\$(dirname '$remote_dir')
  [ -d '$remote_dir' ] && echo dir=exists
  { [ -w '$remote_dir' ] || { [ ! -e '$remote_dir' ] && [ -w \"\$parent\" ]; }; } && echo writable=yes
  [ -d \"\$parent\" ] || echo parent=missing
  [ -f '$remote_dir/.env' ] && echo env=yes && grep -s '^KANBOT_ALLOWED_HOSTS=' '$remote_dir/.env'
  [ -f '$remote_dir/data/kanban.db' ] && echo db=yes
  command -v curl >/dev/null && curl -s --max-time 3 http://localhost:$port/api | grep -o '\"version\": *\"[^\"]*\"' | sed 's/^/running=/'
  true
")
has() { grep -q "^$1" <<<"$facts"; }

has docker=yes && ok "docker is installed" || fail "docker not found. Install Container Manager from Package Center."
if has compose=plugin; then ok "docker compose is available"
elif has compose=standalone; then ok "docker-compose is available"
else fail "neither 'docker compose' nor 'docker-compose' found"; fi
has tar=yes || fail "tar not found"
if has parent=missing; then fail "$(dirname "$remote_dir") doesn't exist. Create the shared folder first."
elif has writable=yes; then ok "$remote_dir is writable"
else fail "$remote_dir isn't writable by $target"; fi
has dir=exists && ok "$remote_dir exists" || warn "$remote_dir will be created"
if has env=yes; then
  hosts=$(sed -n 's/^KANBOT_ALLOWED_HOSTS=//p' <<<"$facts")
  [ -n "$hosts" ] && ok ".env allows: $hosts" || warn ".env has no KANBOT_ALLOWED_HOSTS; only localhost will work"
else
  warn ".env missing; the first deploy creates it from .env.example for you to edit"
fi
has db=yes && ok "data/kanban.db exists and will be kept" || warn "no database yet; a new one will be created"
running=$(sed -n 's/^running="version": *"\(.*\)"/\1/p' <<<"$facts")
[ -n "$running" ] && ok "Kanbot $running is running on port $port" || warn "nothing answering on port $port yet"

if [ "$failed" = 1 ]; then echo; echo "Fix the failures above, then run again." >&2; exit 1; fi
if [ "$mode" = check ]; then echo; echo "Ready to deploy."; exit 0; fi

steps="copy $ref ($commit) into $remote_dir, replacing src/ and keeping .env and data/
build the image as kanbot:${version:-latest} and restart the container with sudo
wait for the container's health check and report the running version"
if [ "$mode" = dry-run ]; then
  echo; echo "A deploy would:"; sed 's/^/  - /' <<<"$steps"; exit 0
fi

echo; echo "Deploying $ref ($commit)"
# Clear old source first, so files deleted from the repo don't linger in the image.
git -C "$repo" archive --format=tar "$ref" | remote "
  set -e
  mkdir -p '$remote_dir/data'
  rm -rf '$remote_dir/src'
  tar -x -C '$remote_dir'
"
ok "code copied"

# sudo needs a terminal for the password prompt, so this step is interactive.
ssh -t -o ConnectTimeout=10 "$target" "
  set -e
  export PATH=/usr/local/bin:/usr/bin:/bin:\$PATH
  cd '$remote_dir'
  if [ ! -f .env ]; then
    cp .env.example .env
    echo 'Created .env from .env.example. Edit KANBOT_ALLOWED_HOSTS in $remote_dir/.env, then run this again.'
    exit 3
  fi
  if sudo docker compose version >/dev/null 2>&1; then compose='sudo docker compose'; else compose='sudo docker-compose'; fi
  export KANBOT_VERSION='${version:-latest}'
  \$compose up -d --build
  echo 'Waiting for the health check...'
  for i in \$(seq 1 30); do
    status=\$(sudo docker inspect -f '{{.State.Health.Status}}' kanbot 2>/dev/null || echo missing)
    [ \"\$status\" = healthy ] && break
    sleep 2
  done
  if [ \"\$status\" != healthy ]; then
    echo \"Container isn't healthy (\$status). Recent logs:\"
    sudo docker logs --tail 30 kanbot
    exit 4
  fi
  curl -s --max-time 3 http://localhost:$port/api | grep -o '\"version\": *\"[^\"]*\"' || true
" && echo "Deployed $ref. Open http://${target#*@}:$port"
