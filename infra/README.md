# API on AWS: setup and deploy runbook

How the API runs in production (ADR-030, ADR-031): one Docker image, one ECS Fargate task in Singapore
(`ap-southeast-1`), behind a load balancer at `https://api.hackerlab.network`. Postgres is Supabase, Redis is
Upstash, and the web app is on Vercel.

```
browser → Vercel (web) → /api/* → Cloudflare DNS (grey cloud) → ALB :443 → ECS task :8000 → Supabase + Upstash
```

There are two tools, each with its own job:

| Path | Job | When |
|---|---|---|
| `terraform/` | Builds the pieces that rarely change: network, load balancer, certificate, Cloudflare records, roles, secret boxes, cluster, service. | Once, then whenever you change a `.tf` file |
| `scripts/set-secrets.sh` | Puts the secret values in their boxes, with hidden input. | Once, and when a secret changes |
| `scripts/deploy.sh` | Ships the code: build → push → migrate → roll → check `/health`. | Every release |

Terraform owns the house, and `deploy.sh` swaps the furniture: Terraform never touches which image runs.

Cost: about $40–50 a month. That covers the load balancer (~$18), the task (0.5 vCPU / 1 GiB ARM, ~$12), public
IPv4 addresses (~$11), three secrets ($1.20) and the state bucket (cents).

## What you need first

1. **An AWS CLI profile named `agentspace`** with admin rights:
   - In the console, go to IAM → Users → Create user `agentspace-deploy` and attach `AdministratorAccess`.
   - On that user, open Security credentials → Create access key → CLI.
   - Then:
     ```sh
     aws configure --profile agentspace   # region ap-southeast-1, output json
     aws sts get-caller-identity --profile agentspace
     ```
2. **A Cloudflare API token:**
   - In Cloudflare, go to My Profile → API Tokens → Create Token → "Edit zone DNS" template.
   - Zone Resources: Include → Specific zone → `hackerlab.network`.
   - Keep it in your shell only: `export CLOUDFLARE_API_TOKEN=...`. Never commit it.
3. **Tools:** `brew install hashicorp/tap/terraform`, Docker Desktop running, plus `jq` and `openssl` (already on
   macOS or Homebrew).
4. **The two connection URLs** at hand. `set-secrets.sh` asks for them:
   - `AGENTSPACE_DATABASE_URL`: in Supabase, go to Connect → **Session pooler** (port 5432, not the direct
     connection: Fargate has no IPv6), and change `postgresql://` to `postgresql+asyncpg://`.
   - `AGENTSPACE_REDIS_URL`: the Upstash `rediss://default:…@….upstash.io:6379` URL.

## First-time setup

```sh
export AWS_PROFILE=agentspace CLOUDFLARE_API_TOKEN=...   # in this terminal only
infra/terraform/bootstrap.sh            # makes the state bucket, runs `terraform init`
cd infra/terraform
terraform plan                          # read it: about 30 things to add, nothing to change or destroy
terraform apply                         # type "yes"; the certificate takes a few minutes
cd ../..
infra/scripts/set-secrets.sh            # asks for the database and Redis URLs, generates the secret key
infra/scripts/deploy.sh                 # the first deploy starts the first task
```

**Never replace or delete `agentspace/AGENTSPACE_SECRET_KEY`.** It encrypts users' saved provider keys: a new one
makes them all unreadable (ADR-030 §7). Terraform refuses to destroy its box, and `set-secrets.sh` refuses to
replace it.

## Every deploy

Run it from a clean, committed checkout of the code you want to ship, normally `dev` after a merge:

```sh
infra/scripts/deploy.sh
```

1. **Build** the image for ARM64, tagged with the commit (`abc1234`).
2. **Push** it to ECR.
3. **Register** a task definition: a copy of the latest one (Terraform's settings) with the new image.
4. **Migrate**: a one-off task runs `alembic upgrade head` and the script prints its log. **If it fails, the deploy
   stops here** and the running service is untouched.
5. **Roll**:
   - ECS starts the new task. Once `/health` passes, the load balancer sends it new requests.
   - The old task gets up to 120 s for its open streams, then 100 s for running replies, before it stops (ADR-030 §3).
   - If the new task never gets healthy, ECS rolls back to the old one by itself, and the script says so.

A deploy takes about 5–8 minutes, most of it waiting for the old task to drain.

### Migrations must be safe for the old code

For a few minutes, the old and new servers both use the migrated database. So a migration may only **add**: a new
table, a new nullable column, a new index. To remove or rename something, do it in two deploys:

1. Deploy code that no longer uses the old column.
2. Deploy the migration that drops it.

## Changing the setup

Edit the `.tf` files and run `terraform plan`, then `terraform apply`, from `infra/terraform/`. Don't change things
in the AWS console: the next `apply` puts them back.

- **A new setting or secret** for the API: edit `ecs.tf` (and `secrets.tf`), apply, run `set-secrets.sh` if you
  added a secret, then deploy. The new task definition only goes live on the next deploy.
- **Tracing:** `terraform apply -var langfuse_enabled=true`, then run `set-secrets.sh` and deploy.

## After the first deploy

- **Vercel:** create the project from this repo with root directory `apps/web`, and set
  `API_URL=https://api.hackerlab.network`.
- **Supabase:** go to Auth → URL Configuration and add the Vercel URL to the redirect URLs.
- Check that a long reply streams through Vercel's rewrite without being cut (ADR-030 §8).

## When something goes wrong

With `export AWS_PROFILE=agentspace AWS_REGION=ap-southeast-1` set:

| What | How |
|---|---|
| See live logs | `aws logs tail /ecs/agentspace-api --follow` |
| Is it up? | `curl https://api.hackerlab.network/health` |
| Roll back the code | `aws ecs list-task-definitions --family-prefix agentspace-api --sort DESC --max-items 3`, then `aws ecs update-service --cluster agentspace --service agentspace-api --task-definition agentspace-api:<previous number>` |
| Restart the task | `aws ecs update-service --cluster agentspace --service agentspace-api --force-new-deployment` |
| Change a secret | `infra/scripts/set-secrets.sh AGENTSPACE_REDIS_URL` (asks for the new value), then restart the task |
| Bad `terraform apply` | The state bucket is versioned: an older state can be restored from the S3 console |

A rollback only rolls back the code, not the database. That's one more reason a migration only adds: the old code
still works on the new tables.

## What's left out on purpose

- **No NAT gateway.** The task gets a public IP to reach ECR, Supabase and Upstash. Its firewall only lets the load
  balancer in on port 8000.
- **No autoscaling.** One task until workspace files leave its disk (sandboxes, ADR-022 step 28). Workspace files are
  lost on each deploy until then.
- **No CI.** Terraform and deploys run from a laptop. `terraform fmt`/`validate` aren't in `pnpm check` yet.
