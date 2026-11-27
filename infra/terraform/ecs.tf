# The API itself: image registry, logs, the role ECS runs it with, the task definition and the service (ADR-030).
# Terraform owns all of it except which image runs: `infra/scripts/deploy.sh` copies the latest task definition
# with a new image, migrates, and rolls the service, as CompanyOS's `ecs-deploy.sh` does.

resource "aws_ecr_repository" "api" {
  name = "agentspace-api"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Keep the last 20 images: enough to roll back, without paying to store every build.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/agentspace-api"
  retention_in_days = 30
}

# The role ECS uses to start the task: pull the image, write logs, read our secrets (and nothing else).
resource "aws_iam_role" "execution" {
  name = "agentspace-api-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "read_secrets" {
  name = "read-agentspace-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = values(local.secret_arns)
    }]
  })
}

resource "aws_ecs_cluster" "this" {
  name = "agentspace"
}

# The settings recipe. `deploy.sh` registers a copy with the new image on every deploy, so a change here goes live
# on the next deploy. Until the first deploy it points at a tag that doesn't exist, which is why the service starts
# with 0 tasks.
resource "aws_ecs_task_definition" "api" {
  family                   = "agentspace-api"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn

  # ARM: the image builds natively on an Apple Silicon Mac, and Fargate ARM costs about 20% less.
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name         = "api"
    image        = "${aws_ecr_repository.api.repository_url}:not-deployed-yet"
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    # SIGKILL comes 120 s after SIGTERM: 100 s of grace for runs, then 15 s for the last saves (ADR-030 §3).
    stopTimeout = 120
    environment = concat(
      [
        { name = "AGENTSPACE_SUPABASE_URL", value = var.supabase_url },
        { name = "AGENTSPACE_AUTO_MIGRATE", value = "false" },
        { name = "AGENTSPACE_SHUTDOWN_GRACE_SECONDS", value = "100" },
      ],
      # Each session's files and commands in its own Daytona sandbox, named apart from dev ones (ADR-035).
      var.sandboxes_enabled ? [
        { name = "AGENTSPACE_WORKSPACE_BACKEND", value = "daytona" },
        { name = "AGENTSPACE_SANDBOX_PREFIX", value = "agentspace-prod" },
      ] : [],
    )
    secrets = [for name, arn in local.secret_arns : { name = name, valueFrom = arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "ecs"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "agentspace-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  launch_type     = "FARGATE"
  # 0 until the first deploy pushes an image; `deploy.sh` sets 1. One task: workspace files are on its disk (§1).
  desired_count = 0
  # A new task gets 60 s to start before failed health checks count against it.
  health_check_grace_period_seconds = 60

  # The new task starts and passes /health before the old one stops.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  # If the new task never gets healthy, ECS goes back to the old one by itself.
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = data.aws_subnets.default.ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # `deploy.sh` owns which revision runs and how many tasks; Terraform leaves both alone after creating the service.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.https]
}
