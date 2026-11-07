#!/usr/bin/env bash
# Install or update the kanban board as a systemd user service, then restart it.
# Run this again after pulling changes to apply them.
set -euo pipefail

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
url=http://127.0.0.1:8787/api
unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user

for tool in uv systemctl curl; do
  command -v "$tool" >/dev/null || { echo "error: $tool is required" >&2; exit 1; }
done
uv=$(command -v uv)

# Escape the paths for use as sed replacements.
escape() { printf '%s' "$1" | sed 's/[&|\]/\\&/g'; }

mkdir -p "$unit_dir"
sed -e "s|@REPO@|$(escape "$repo")|" -e "s|@UV@|$(escape "$uv")|" \
  "$repo/systemd/kanban.service" > "$unit_dir/kanban.service"
chmod 644 "$unit_dir/kanban.service"

systemctl --user daemon-reload
systemctl --user enable --quiet kanban
systemctl --user restart kanban

# A failed start only shows up once the process has tried to bind the port.
for _ in {1..15}; do
  sleep 1
  state=$(systemctl --user show kanban -p SubState --value)
  if curl -sf "$url" >/dev/null; then
    if [ "$state" = running ]; then
      sleep 1
      state=$(systemctl --user show kanban -p SubState --value)
    fi
    if [ "$state" = running ]; then
      echo "Kanban is running at http://127.0.0.1:8787"
    else
      echo "Installed, but another process is using port 8787."
      echo "Stop it, for example with Ctrl-C in its terminal, and the service takes over within 10 seconds."
    fi
    exit 0
  fi
done

echo "error: the service didn't start. Check: journalctl --user -u kanban" >&2
exit 1
