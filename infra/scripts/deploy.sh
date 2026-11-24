#!/usr/bin/env bash
# Deploy the API (ADR-030, ADR-031): build the image, push it to ECR, copy the latest task definition with the new
# image, run the migrations as a one-off task, then roll the service and wait until it's healthy.
# Terraform (`infra/terraform/`) must have been applied and the secrets set (`set-secrets.sh`) first.
set -euo pipefail
source "$(dirname "$0")/config.sh"
cd "$(git rev-parse --show-toplevel)"

# The image is tagged with the commit, so every deploy can be traced back to its code.
[[ -z "$(git status --porcelain)" || "${ALLOW_DIRTY:-}" == 1 ]] ||
  die "uncommitted changes would be deployed untraceably; commit them, or set ALLOW_DIRTY=1"
TAG="$(git rev-parse --short HEAD)$([[ -n "$(git status --porcelain)" ]] && echo -dirty || true)"
IMAGE="$ECR_URI:$TAG"

step "1/5 Build $IMAGE"
# ARM64 to match the task definition's runtimePlatform (ecs.tf).
docker build --platform linux/arm64 -t "$IMAGE" apps/api

step "2/5 Push to ECR"
aws ecr get-login-password | docker login --username AWS --password-stdin "${ECR_URI%%/*}" >/dev/null
docker push "$IMAGE"

step "3/5 Register a task definition with the new image"
# The latest revision holds the newest settings: Terraform's after a change in ecs.tf, else the last deploy's.
# The read-only fields AWS adds are dropped, so it can be registered again.
TASK_DEF="$(aws ecs describe-task-definition --task-definition "$APP" --query taskDefinition --output json |
  jq --arg image "$IMAGE" '.containerDefinitions[0].image = $image
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities,
          .registeredAt, .registeredBy, .deregisteredAt)')"
TASK_DEF_ARN="$(aws ecs register-task-definition --cli-input-json "$TASK_DEF" --query taskDefinition.taskDefinitionArn --output text)"
echo "registered: $TASK_DEF_ARN"

step "4/5 Migrate the database (one-off task: alembic upgrade head)"
# Same subnets and firewall as the service, which Terraform set.
NETWORK="$(aws ecs describe-services --cluster "$CLUSTER" --services "$APP" --query 'services[0].networkConfiguration' --output json)"
MIGRATE_TASK="$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TASK_DEF_ARN" \
  --network-configuration "$NETWORK" --started-by deploy-migrate \
  --overrides '{"containerOverrides":[{"name":"api","command":["alembic","upgrade","head"]}]}' \
  --query 'tasks[0].taskArn' --output text)"
echo "started: $MIGRATE_TASK (waiting for it to finish...)"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$MIGRATE_TASK"
read -r EXIT_CODE STOP_REASON < <(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$MIGRATE_TASK" \
  --query 'tasks[0].[containers[0].exitCode, stoppedReason]' --output text)
echo "--- migration log ---"
aws logs get-log-events --log-group-name "$LOG_GROUP" --log-stream-name "ecs/api/${MIGRATE_TASK##*/}" \
  --query 'events[].message' --output text 2>/dev/null | tr '\t' '\n' || echo "(no log yet)"
echo "---------------------"
# A failed migration stops here, before any task runs the new code (ADR-030 §4).
[[ "$EXIT_CODE" == 0 ]] || die "migration failed (exit $EXIT_CODE: $STOP_REASON); the service was not changed"

step "5/5 Roll the service onto $TAG"
# desired-count 1 also starts the very first task: Terraform creates the service with 0 (ecs.tf).
aws ecs update-service --cluster "$CLUSTER" --service "$APP" --task-definition "$TASK_DEF_ARN" --desired-count 1 >/dev/null
echo "waiting for the new task to be healthy and the old one to drain (up to ~5 min)..."
aws ecs wait services-stable --cluster "$CLUSTER" --services "$APP"
RUNNING="$(aws ecs describe-services --cluster "$CLUSTER" --services "$APP" --query 'services[0].deployments[?status==`PRIMARY`].taskDefinition | [0]' --output text)"
[[ "$RUNNING" == "$TASK_DEF_ARN" ]] || die "ECS rolled back to $RUNNING: the new task never got healthy. Logs: aws logs tail $LOG_GROUP --since 15m"

echo
curl -fsS "https://$API_DOMAIN/health" && echo && echo "Deployed $TAG to https://$API_DOMAIN" ||
  echo "Deployed $TAG, but https://$API_DOMAIN/health didn't answer yet: DNS may still be spreading, try again in a minute"
