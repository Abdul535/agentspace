#!/usr/bin/env bash
# Once per AWS account: make the S3 bucket that holds Terraform's state. Once per machine: `terraform init` against
# it. Safe to re-run. The bucket is made here, not in Terraform, because Terraform needs it before it can run.
set -euo pipefail
source "$(dirname "$0")/../scripts/config.sh"
cd "$(dirname "$0")"

BUCKET="agentspace-tfstate-$ACCOUNT_ID"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "exists: s3://$BUCKET"
else
  aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  # Versioning keeps every old state, so a bad apply or a deleted state can be undone.
  aws s3api put-bucket-versioning --bucket "$BUCKET" --versioning-configuration Status=Enabled
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "created: s3://$BUCKET (versioned, private)"
fi

terraform init -backend-config="bucket=$BUCKET"
