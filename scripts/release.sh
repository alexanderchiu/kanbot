#!/usr/bin/env bash
# Cut a release: check, bump the version, date the changelog, commit, tag and push.
#
#   scripts/release.sh patch|minor|major|X.Y.Z [--no-push]
#
# Needs a clean main branch with notes under "## Unreleased" in CHANGELOG.md.
# Set RELEASE_TRAILER to add a trailer line to the release commit.
set -euo pipefail

bump=${1:?usage: scripts/release.sh patch|minor|major|X.Y.Z [--no-push]}
push=yes; [ "${2:-}" = --no-push ] && push=no
cd "$(dirname "${BASH_SOURCE[0]}")/.."

[ "$(git branch --show-current)" = main ] || { echo "error: release from main" >&2; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "error: commit or stash changes first" >&2; exit 1; }
notes=$(awk '/^## Unreleased/{on=1; next} /^## /{on=0} on' CHANGELOG.md | sed '/./,$!d')
[ -n "$(tr -d '[:space:]' <<<"$notes")" ] || { echo "error: nothing under ## Unreleased in CHANGELOG.md" >&2; exit 1; }

echo "Running checks..."
uv run --quiet pytest -q
uv run --quiet ruff check
uv run --quiet ruff format --check

case $bump in
  patch|minor|major) uv version --quiet --bump "$bump" ;;
  [0-9]*.[0-9]*.[0-9]*) uv version --quiet "$bump" ;;
  *) echo "error: expected patch, minor, major or X.Y.Z" >&2; exit 1 ;;
esac
version=$(uv version --short)
git rev-parse -q --verify "refs/tags/v$version" >/dev/null && { echo "error: v$version already exists" >&2; git checkout -- pyproject.toml uv.lock; exit 1; }

# Date the Unreleased notes, and start a fresh empty Unreleased section above them.
sed -i "s/^## Unreleased$/## Unreleased\n\n## [$version] - $(date +%F)/" CHANGELOG.md

git add pyproject.toml uv.lock CHANGELOG.md
git commit -q -m "Release $version" -m "$(printf '%s\n' "$notes")" ${RELEASE_TRAILER:+--trailer "$RELEASE_TRAILER"}
git tag -a "v$version" -m "Kanbot $version" -m "$notes"
echo "Tagged v$version."

if [ "$push" = yes ]; then
  # Push to the branch main tracks (master on GitHub), not a new branch named main.
  upstream=$(git rev-parse --abbrev-ref '@{upstream}')
  git push -q "${upstream%%/*}" "HEAD:${upstream#*/}" --follow-tags
  echo "Pushed v$version."
else
  echo "Not pushed. Push with: git push --follow-tags"
fi
