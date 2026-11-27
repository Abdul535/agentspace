#!/usr/bin/env bash
# Fill the secret boxes Terraform made (`infra/terraform/secrets.tf`) with their values. Asks only for the ones still
# empty, with hidden input, so values never pass through Terraform, the shell history or a chat.
# Pass a name to replace that one secret's value: `infra/scripts/set-secrets.sh AGENTSPACE_REDIS_URL`.
set -euo pipefail
source "$(dirname "$0")/config.sh"

# Whether the secret already holds a value.
has_value() { aws secretsmanager get-secret-value --secret-id "$SECRET_PREFIX$1" --query VersionId --output text >/dev/null 2>&1; }
# Ask for a value without echoing it, check how it starts, and store it.
ask() {
  local name="$1" prefix="$2" value
  read -rsp "Paste $name (starts with '$prefix'; input is hidden): " value
  echo
  [[ "$value" == "$prefix"* ]] || die "$name must start with '$prefix'"
  aws secretsmanager put-secret-value --secret-id "$SECRET_PREFIX$name" --secret-string "$value" >/dev/null
  echo "set: $SECRET_PREFIX$name"
}

# What a secret's value starts with (a function, not a map: macOS ships bash 3.2, which has no maps).
prefix_of() {
  case "$1" in
    AGENTSPACE_DATABASE_URL) echo "postgresql+asyncpg://" ;;
    AGENTSPACE_REDIS_URL) echo "rediss://" ;;
    LANGFUSE_PUBLIC_KEY) echo "pk-lf-" ;;
    LANGFUSE_SECRET_KEY) echo "sk-lf-" ;;
    DAYTONA_API_KEY) echo "dtn_" ;;
  esac
}

if [[ $# -gt 0 ]]; then
  [[ "$1" != AGENTSPACE_SECRET_KEY ]] || die "AGENTSPACE_SECRET_KEY is never replaced: saved keys would become unreadable"
  [[ -n "$(prefix_of "$1")" ]] || die "unknown secret $1"
  ask "$1" "$(prefix_of "$1")"
  echo "Restart the task to use it: aws ecs update-service --cluster $CLUSTER --service $APP --force-new-deployment"
  exit 0
fi

for name in AGENTSPACE_DATABASE_URL AGENTSPACE_REDIS_URL LANGFUSE_PUBLIC_KEY LANGFUSE_SECRET_KEY DAYTONA_API_KEY; do
  if ! aws secretsmanager describe-secret --secret-id "$SECRET_PREFIX$name" >/dev/null 2>&1; then
    echo "skipped: $SECRET_PREFIX$name (Terraform didn't make it)"
  elif has_value "$name"; then
    echo "already set: $SECRET_PREFIX$name"
  else
    ask "$name" "$(prefix_of "$name")"
  fi
done

# Generated here, never shown, and set only once (ADR-030 §7).
if has_value AGENTSPACE_SECRET_KEY; then
  echo "already set: ${SECRET_PREFIX}AGENTSPACE_SECRET_KEY (never replace it)"
else
  aws secretsmanager put-secret-value --secret-id "${SECRET_PREFIX}AGENTSPACE_SECRET_KEY" \
    --secret-string "$(openssl rand -base64 32)" >/dev/null
  echo "generated: ${SECRET_PREFIX}AGENTSPACE_SECRET_KEY (never replace it)"
fi
