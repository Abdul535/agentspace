# The secret settings' boxes in Secrets Manager (ADR-030 §7). Terraform makes the boxes only; their values are
# set by `infra/scripts/set-secrets.sh`, so no secret ever lands in Terraform's state file.

locals {
  # Each secret is named after the environment variable it fills.
  secret_names = concat(
    ["AGENTSPACE_DATABASE_URL", "AGENTSPACE_REDIS_URL"],
    var.langfuse_enabled ? ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"] : [],
    var.sandboxes_enabled ? ["DAYTONA_API_KEY"] : [],
  )
}

resource "aws_secretsmanager_secret" "settings" {
  for_each = toset(local.secret_names)
  name     = "agentspace/${each.key}"
}

# Its own resource so it can't be destroyed: a new key makes every saved provider key unreadable.
resource "aws_secretsmanager_secret" "secret_key" {
  name        = "agentspace/AGENTSPACE_SECRET_KEY"
  description = "Encrypts users' saved keys. Set once, never change or delete."

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  # Env var name → secret ARN, for the task definition.
  secret_arns = merge(
    { for name, s in aws_secretsmanager_secret.settings : name => s.arn },
    { AGENTSPACE_SECRET_KEY = aws_secretsmanager_secret.secret_key.arn },
  )
}
