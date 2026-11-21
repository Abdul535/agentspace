#!/usr/bin/env bash
# Starts the local Supabase Postgres and Redis if needed and checks this checkout's database (ADR-027, ADR-029).
# Every checkout shares the `postgres` database unless it opted out with --own.
# `pnpm dev:api` and scripts/worktree-setup.sh run it; it is safe to run again by hand.
# Usage: scripts/db.sh            server + this checkout's database (from AGENTSPACE_DATABASE_URL)
#        scripts/db.sh --server   server only (the tests make their own databases)
#        scripts/db.sh --own      give this worktree its own database (`pnpm db:own`)
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
# shellcheck source=/dev/null
[ -f .worktree.env ] && . ./.worktree.env

container=supabase_db_agentspace
default_url="postgresql+asyncpg://postgres:postgres@127.0.0.1:55322/postgres"

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

# Redis holds run events and stop/steer requests, so several API servers can share runs (ADR-029).
# One container for every checkout, like Postgres: session ids are UUIDs, so keys never collide.
redis_container=agentspace_redis
case "$(docker inspect -f '{{.State.Running}}' "$redis_container" 2>/dev/null)" in
  true) ;;
  false) docker start "$redis_container" >/dev/null ;;
  *)
    echo "Starting the local Redis (the first time pulls its image)..."
    docker run -d --name "$redis_container" --restart unless-stopped -p 127.0.0.1:55379:6379 redis:8.2-alpine >/dev/null
    ;;
esac
for _ in $(seq 1 30); do
  [ "$(docker exec "$redis_container" redis-cli ping 2>/dev/null)" = "PONG" ] && break
  sleep 1
done

[ "${1:-}" = "--server" ] && exit 0

if [ "${1:-}" = "--own" ]; then
  main="$(git worktree list --porcelain | head -1 | sed 's/^worktree //')"
  if [ "$(pwd)" = "$main" ]; then
    echo "The main checkout always uses the shared \`postgres\` database." >&2
    exit 1
  fi
  if [ -z "${AGENTSPACE_DATABASE_URL:-}" ]; then
    # Postgres names: lowercase letters, digits and `_`, at most 63 characters.
    slug="$(git rev-parse --abbrev-ref HEAD | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9\n' '_')"
    AGENTSPACE_DATABASE_URL="${default_url%/*}/$(printf 'agentspace_%s' "$slug" | cut -c1-63)"
    echo "export AGENTSPACE_DATABASE_URL=$AGENTSPACE_DATABASE_URL" >>.worktree.env
    echo "This worktree now uses its own database; delete the AGENTSPACE_DATABASE_URL line in .worktree.env to share again."
  fi
fi

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

# On the shared database, a migration that dev doesn't have would stop every other checkout's API
# ("Can't locate revision"): refuse, and say how to opt out.
if [ "$name" = "postgres" ]; then
  base="$(git rev-parse --verify -q origin/dev || git rev-parse --verify -q dev || true)"
  dir=apps/api/migrations/versions
  if [ -n "$base" ]; then
    new="$(comm -13 <(git ls-tree --name-only "$base" "$dir/" | xargs -n1 basename | sort) \
      <(find "$dir" -maxdepth 1 -name '*.py' -exec basename {} \; | sort))"
    if [ -n "$new" ]; then
      echo "This branch has migrations dev doesn't: $(echo "$new" | tr '\n' ' ')" >&2
      echo "Running them on the shared database would stop the other checkouts' API." >&2
      echo "Give this worktree its own database first: pnpm db:own" >&2
      exit 1
    fi
  fi
fi

if [ "$(docker exec "$container" psql -U postgres -tAc "select 1 from pg_database where datname = '$name'")" != "1" ]; then
  docker exec "$container" psql -U postgres -q -c "create database \"$name\""
  echo "Created database $name"
fi
