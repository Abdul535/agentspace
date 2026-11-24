# ADR-031 — The API's AWS setup in Terraform; deploys stay a script

- **Status:** Accepted
- **Date:** 2026-11-22
- **Deciders:** project owner

## Context
ADR-030 §4 set the AWS pieces up by hand (console or CLI) and left infrastructure as code for when there is more
than one service. Nothing exists in AWS yet, so switching now costs nothing to migrate. A hand-made setup can't show
what it would change before it does it, can't put a setting changed in the console back, and needed two DNS records
added by hand in Cloudflare.

CompanyOS (`infra/terraform/`, `infra/GITOPS.md`) splits the work in two:
- **Terraform** owns the long-lived pieces: network, load balancer, certificate, Cloudflare DNS records
  (`modules/acm-cloudflare`, `modules/cloudflare-dns`), IAM roles, Secrets Manager entries and the ECS service.
- **A script** (`infra/scripts/ecs-deploy.sh`) does each release. It registers a task definition with the new
  image, migrates, and rolls the service. The service ignores Terraform's task definition
  (`ignore_changes = [task_definition, desired_count]` in `modules/ecs-api/main.tf`).
- State is in an S3 bucket in the same region, locked with `use_lockfile`.

## Decision
**In one line:** Terraform in `infra/terraform/` builds everything in ADR-030 §4 plus the Cloudflare records, and
`infra/scripts/deploy.sh` ships each image, as CompanyOS does.

- **One root, no modules.** There is one environment (prod). Modules come with a second one.
- **State** lives in `s3://agentspace-tfstate-<account id>` in `ap-southeast-1`, versioned, private and locked by a
  lock file (Terraform ≥ 1.10). `infra/terraform/bootstrap.sh` makes the bucket, because Terraform needs it before
  it can run.
- **Cloudflare** is managed by its provider: the ACM validation record and `api` → ALB, both DNS only. The token
  comes from `CLOUDFLARE_API_TOKEN` and needs Zone → DNS → Edit on the one zone.
- **Secrets:** Terraform makes the boxes and `infra/scripts/set-secrets.sh` fills them (hidden input;
  `AGENTSPACE_SECRET_KEY` generated). That way no secret value is ever in the state file.
  `AGENTSPACE_SECRET_KEY`'s box has `prevent_destroy`.
- **Task definition:** Terraform owns the settings: size, ARM64, `stopTimeout`, environment and secrets. Its image
  tag is a placeholder. `deploy.sh` copies the **latest** revision, sets the image and registers it. A change in
  `ecs.tf` goes live on the next deploy.
- **The service** is created with `desiredCount = 0`, since no image exists yet, and `ignore_changes` on
  `task_definition` and `desired_count`. `deploy.sh` sets both.
- **Applied by hand** (`terraform plan`, then `apply`) from the owner's laptop. No CI apply yet.

The rest of ADR-030 stands: one task, the drain timings, Upstash, Supabase's session pooler, Vercel.

## Alternatives considered
- **Keep the CLI script** (`setup.sh`, ADR-030 §4). Less to learn, but it can only create, not compare or fix, and
  every step needs its own "does it exist?" check.
- **AWS CDK or Pulumi in TypeScript.** Familiar language for the owner. But CompanyOS, the reference design, uses
  Terraform, and Terraform has the most examples. CDK also hides the plan behind CloudFormation.
- **Terraform owns the image too** (`terraform apply -var image_tag=…` per deploy). One tool, but every deploy
  becomes a full plan of the whole account, and the migration still has to run between two steps.
- **Secret values in Terraform** (`random_password`, `secret_string`). Fewer scripts, but the values sit in the
  state file in plain text.

## Consequences
- `terraform plan` shows every change before it happens, and `terraform destroy` removes everything except the
  secret key's box, by design.
- Terraform must be installed (`brew install hashicorp/tap/terraform`), plus a Cloudflare API token with DNS edit
  rights on `hackerlab.network`.
- A setting changed in the console is undone by the next `apply`. Change `infra/terraform/` instead.
- `terraform fmt -check` and `validate` are not in `pnpm check` or CI yet. Adding them needs Terraform in CI and on
  every machine that commits.
- ADR-030 §4's "by hand" is superseded by this ADR.
