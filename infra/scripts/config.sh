# Shared settings for the infra scripts (ADR-030, ADR-031). Sourced, not run.
# Override any of them from the environment, e.g. `AWS_PROFILE=other infra/scripts/deploy.sh`.

export AWS_PROFILE="${AWS_PROFILE:-agentspace}"
# Singapore: the same region as Upstash Redis and Supabase Postgres. Must match `infra/terraform/variables.tf`.
export AWS_REGION="${AWS_REGION:-ap-southeast-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
# Print plain values, never open a pager.
export AWS_PAGER=""

API_DOMAIN="${API_DOMAIN:-api.hackerlab.network}"
# These names are made by Terraform (`infra/terraform/`).
APP=agentspace-api
CLUSTER=agentspace
LOG_GROUP=/ecs/agentspace-api
SECRET_PREFIX=agentspace/

# Step out with a message on stderr.
die() { echo "error: $*" >&2; exit 1; }
# A step header, so the output reads as a checklist.
step() { echo; echo "==> $*"; }

# The account id, which also proves the profile works.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" ||
  die "AWS profile '$AWS_PROFILE' doesn't work. Run: aws configure --profile $AWS_PROFILE"
ECR_URI="$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$APP"
