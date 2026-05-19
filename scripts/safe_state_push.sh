#!/usr/bin/env bash
# Commit and push config.json changes with a small rebase-retry loop.
#
# Since the D1 cutover (Phase F), the only file the bot ever rewrites is
# config.json, and only when /reset (or another category-mutation command)
# fires. Two concurrent workflow runs touching config.json is extremely
# unlikely — the concurrency group on `telegram-poll` already serializes
# them — but on the off chance we lose the race, we rebase against origin
# and try again. There's no semantic merge step anymore: config.json only
# carries a small ``categories`` array, and the latest write always wins.
#
# Usage:   scripts/safe_state_push.sh "<commit message>"
# Env:     GITHUB_REF_NAME (defaults to "main") names the branch to push to.
#          LITFEED_PUSH_ATTEMPTS (default 5) caps the retry count.

set -uo pipefail

MSG="${1:?commit message required}"
BRANCH="${GITHUB_REF_NAME:-main}"
ATTEMPTS="${LITFEED_PUSH_ATTEMPTS:-5}"

STATE_FILE="config.json"

if [[ -z "$(git status --porcelain -- "$STATE_FILE")" ]]; then
  echo "[safe_state_push] No config.json changes."
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for attempt in $(seq 1 "$ATTEMPTS"); do
  git add "$STATE_FILE"

  if git diff --cached --quiet; then
    echo "[safe_state_push] attempt=$attempt nothing staged; remote already has our changes."
    exit 0
  fi

  git commit -m "$MSG" >/dev/null

  if git push origin "HEAD:${BRANCH}" 2>&1; then
    echo "[safe_state_push] attempt=$attempt pushed."
    exit 0
  fi

  echo "[safe_state_push] attempt=$attempt push failed; rebasing on origin/${BRANCH}."

  git fetch origin "$BRANCH" || {
    echo "[safe_state_push] fetch failed" >&2
    sleep $((attempt * 2))
    continue
  }
  # Reset to remote; our config.json change is the only thing we'll re-apply,
  # and it's already on disk (we haven't reset --hard). Stash + pop survives the rebase.
  git stash push -- "$STATE_FILE" >/dev/null 2>&1 || true
  git reset --hard "origin/${BRANCH}"
  git stash pop >/dev/null 2>&1 || true

  sleep $((attempt * 2))
done

echo "[safe_state_push] FAILED after $ATTEMPTS attempts." >&2
exit 1
