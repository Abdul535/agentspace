#!/usr/bin/env bash
# Prepares a new git worktree to run (ADR-011): copies secrets, installs deps, picks free ports.
# T3 runs it when it creates a worktree; it is safe to run again by hand.
set -euo pipefail

here="$(git rev-parse --show-toplevel)"
# The first entry of `git worktree list` is always the main checkout.
main="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
cd "$here"

if [ "$here" = "$main" ]; then
  echo "This is the main checkout: it keeps the default ports (API 8000, web 3000). Nothing to do."
  exit 0
fi

# Secrets are gitignored, so a new worktree has none: copy them from the main checkout.
for f in apps/api/.env apps/web/.env.local; do
  if [ -f "$main/$f" ] && [ ! -f "$f" ]; then
    cp "$main/$f" "$f"
    echo "Copied $f"
  fi
done

pnpm install --frozen-lockfile
(cd apps/api && uv sync --locked)

# Each worktree takes a slot: API 8000+10n, web 3000+10n. Slot 0 is the main checkout.
if [ ! -f .worktree.env ]; then
  # Ports already claimed by another worktree, even if its servers are not running now.
  taken="$(git worktree list --porcelain | sed -n 's/^worktree //p' | while read -r wt; do
    [ -f "$wt/.worktree.env" ] && grep -h '^export API_PORT=' "$wt/.worktree.env" | cut -d= -f2
  done || true)"
  slot=1
  while :; do
    api=$((8000 + slot * 10))
    web=$((3000 + slot * 10))
    if ! grep -qx "$api" <<<"$taken" && ! lsof -iTCP:"$api" -sTCP:LISTEN >/dev/null 2>&1 &&
      ! lsof -iTCP:"$web" -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    slot=$((slot + 1))
  done
  cat >.worktree.env <<EOF
# Ports for this worktree, read by \`pnpm dev:api\` and \`pnpm dev:web\` (written by scripts/worktree-setup.sh).
export API_PORT=$api
export PORT=$web
export API_URL=http://127.0.0.1:$api
EOF
fi

# shellcheck source=/dev/null
. ./.worktree.env
echo "Ready: API on http://127.0.0.1:$API_PORT, web on http://localhost:$PORT"
