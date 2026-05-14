#!/usr/bin/env bash
# Sync local main with remote, then push.
# Handles divergence caused by GitHub Actions bot commits to config.json.
set -euo pipefail

branch="$(git rev-parse --abbrev-ref HEAD)"
echo "[sync] branch: $branch"

echo "[sync] fetching..."
git fetch origin "$branch"

# pull.rebase=true and rebase.autoStash=true are set repo-locally,
# so this rebases cleanly even with uncommitted work.
echo "[sync] rebasing on origin/$branch..."
git pull origin "$branch"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "[sync] working tree dirty after rebase; not pushing."
  exit 0
fi

ahead="$(git rev-list --count "origin/$branch..HEAD")"
if [[ "$ahead" -eq 0 ]]; then
  echo "[sync] nothing to push."
  exit 0
fi

echo "[sync] pushing $ahead commit(s)..."
git push origin "$branch"
echo "[sync] done."
