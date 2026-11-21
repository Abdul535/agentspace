#!/usr/bin/env bash
# Prepares a new git worktree to run (ADR-011): copies secrets, installs deps, picks free ports and its own database.
# T3 runs it when it creates a worktree; it is safe to run again by hand.
set -euo pipefail

here="$(git rev-parse --show-toplevel)"
# The first entry of `git worktree list` is always the main checkout.
main="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
cd "$here"

if [ "$here" = "$main" ]; then
  echo "This is the main checkout: it keeps the default ports (API 8000, web 3000) and the \`agentspace\` database."
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
# Ports and database for this worktree, read by \`pnpm dev:api\` and \`pnpm dev:web\` (written by scripts/worktree-setup.sh).
export API_PORT=$api
export PORT=$web
export API_URL=http://127.0.0.1:$api
EOF
fi

# Its own database on the shared local Postgres (ADR-027), so two branches' migrations never meet.
# Added to an older .worktree.env too, which only had ports.
if ! grep -q '^export AGENTSPACE_DATABASE_URL=' .worktree.env; then
  # Postgres names: lowercase letters, digits and `_`, at most 63 characters.
  slug="$(git rev-parse --abbrev-ref HEAD | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '_')"
  db="$(printf 'agentspace_%s' "$slug" | cut -c1-63)"
  echo "export AGENTSPACE_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:55322/$db" >>.worktree.env
fi

# Without Docker the worktree is still set up; `pnpm dev:api` tries again and says what's missing.
bash scripts/db.sh || echo "The database isn't ready yet; \`pnpm dev:api\` will create it."

# shellcheck source=/dev/null
. ./.worktree.env
echo "Ready: API on http://127.0.0.1:$API_PORT, web on http://localhost:$PORT, database ${AGENTSPACE_DATABASE_URL##*/}"
