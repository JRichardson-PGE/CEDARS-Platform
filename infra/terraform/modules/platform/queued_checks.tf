# Queued checks: a filing upload (or a Check again) too large to check while the page waits is checked by an AWS Batch job of the web
# image, `manage.py run_queued_checks --check <id>`, with the web tasks' access (the core database, the PII vault for a PII upload,
# the buckets), as the request would have checked it. Each job gets queued_check_memory; the platform submits no more than its
# queued_checks_at_once limit at a time, and calculations_sync submits the next and fails a job Batch ended without finishing it.

locals {
  queued_checks_name = "${local.name}-queued-checks"
}

resource "aws_batch_compute_environment" "queued_checks" {
  compute_environment_name = local.queued_checks_name
  type                     = "MANAGED"
  service_role             = aws_iam_role.batch_service.arn
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = var.queued_check_max_vcpus
    subnets            = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.web.id]
  }
  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.batch_service]
}

resource "aws_batch_job_queue" "queued_checks" {
  name     = local.queued_checks_name
  state    = "ENABLED"
  priority = 10
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.queued_checks.arn
  }
  tags = local.tags
}

resource "aws_batch_job_definition" "queued_checks" {
  name                  = local.queued_checks_name
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true
  retry_strategy {
    attempts = 1
  }
  timeout {
    attempt_duration_seconds = 6 * 3600 # QUEUED_CHECKS_MAX_HOURS
  }
  container_properties = jsonencode({
    image            = var.web_image
    command          = ["python", "manage.py", "run_queued_checks", "--check", "Ref::check_id"]
    jobRoleArn       = aws_iam_role.web.arn
    executionRoleArn = aws_iam_role.execution.arn
    environment      = local.web_environment
    secrets          = local.app_secrets
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.queued_check_vcpu) },
      { type = "MEMORY", value = tostring(var.queued_check_memory) },
    ]
    ephemeralStorage             = { sizeInGiB = 40 }
    networkConfiguration         = { assignPublicIp = "DISABLED" }
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "queued-checks"
      }
    }
  })
  tags = local.tags
}
