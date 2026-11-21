#!/usr/bin/env bash
# Starts the local Supabase Postgres if needed and creates this checkout's database (ADR-027).
# `pnpm dev:api` and scripts/worktree-setup.sh run it; it is safe to run again by hand.
# Usage: scripts/db.sh            server + this checkout's database (from AGENTSPACE_DATABASE_URL)
#        scripts/db.sh --server   server only (the tests make their own databases)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
# shellcheck source=/dev/null
[ -f .worktree.env ] && . ./.worktree.env

container=supabase_db_agentspace
default_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:55322/agentspace"

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running. Start Docker Desktop, then run this again: the database runs in Docker." >&2
  exit 1
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
  if ! command -v supabase >/dev/null 2>&1; then
    echo "The Supabase CLI isn't installed: brew install supabase/tap/supabase" >&2
    exit 1
  fi
  echo "Starting the local Supabase database (the first time pulls its image, about a minute)..."
  supabase start >/dev/null
fi

# `supabase start` returns once the container runs; Postgres may still be opening.
for _ in $(seq 1 30); do
  docker exec "$container" pg_isready -U postgres -q && break
  sleep 1
done

[ "${1:-}" = "--server" ] && exit 0

url="${AGENTSPACE_DATABASE_URL:-$default_url}"
# The database name is the URL's path, without any `?query`.
name="${url##*/}"
name="${name%%\?*}"
case "$url" in
  *@127.0.0.1:55322/* | *@localhost:55322/*) ;;
  *)
    echo "AGENTSPACE_DATABASE_URL isn't the local Supabase database; leaving it alone."
    exit 0
    ;;
esac

if [ "$(docker exec "$container" psql -U postgres -tAc "select 1 from pg_database where datname = '$name'")" != "1" ]; then
  docker exec "$container" psql -U postgres -q -c "create database \"$name\""
  echo "Created database $name"
fi
