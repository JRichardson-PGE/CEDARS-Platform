# The Django application on ECS Fargate behind an HTTPS load balancer with AWS WAF (its background work is in jobs.tf).

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = local.tags
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/cedars/${var.environment}/app"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
  tags              = local.tags
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Starts tasks: pulls images, writes logs, reads the app secret
resource "aws_iam_role" "execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.app.arn },
      { Effect = "Allow", Action = ["kms:Decrypt"], Resource = aws_kms_key.data.arn },
    ]
  })
}

# What the application itself may do: its buckets, and queue calculation jobs
resource "aws_iam_role" "web" {
  name               = "${local.name}-web"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "web" {
  role = aws_iam_role.web.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket", "s3:AbortMultipartUpload"]
        Resource = flatten([for b in aws_s3_bucket.data : [b.arn, "${b.arn}/*"]])
      },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.data.arn },
      {
        Effect   = "Allow"
        Action   = ["batch:SubmitJob", "batch:DescribeJobs", "batch:TerminateJob", "batch:CancelJob"]
        Resource = concat([for q in aws_batch_job_queue.runs : q.arn], ["${aws_batch_job_definition.recet.arn_prefix}*"])
      },
      { Effect = "Allow", Action = ["ses:SendRawEmail"], Resource = "*" },
    ]
  })
}

locals {
  settings_module = var.environment == "sandbox" ? "cedars.settings.sandbox" : "cedars.settings.prod"
  web_environment = [
    { name = "DJANGO_SETTINGS_MODULE", value = local.settings_module },
    { name = "CEDARS_ENVIRONMENT", value = var.environment },
    { name = "DJANGO_ALLOWED_HOSTS", value = var.domain_name },
    { name = "CEDARS_BASE_URL", value = "https://${var.domain_name}" },
    { name = "DEFAULT_FROM_EMAIL", value = "CEDARS <no-reply@${var.domain_name}>" },
    { name = "EMAIL_HOST", value = "email-smtp.${data.aws_region.current.name}.amazonaws.com" },
    { name = "CEDARS_STORAGE_BACKEND", value = "s3" },
    { name = "CALCULATIONS_BACKEND", value = "batch" },
    { name = "CEDARS_BUCKET_UPLOADS", value = aws_s3_bucket.data["uploads"].bucket },
    { name = "CEDARS_BUCKET_RESULTS", value = aws_s3_bucket.data["results"].bucket },
    { name = "CEDARS_BUCKET_REFDATA", value = aws_s3_bucket.data["refdata"].bucket },
    { name = "CEDARS_BUCKET_SNAPSHOTS", value = aws_s3_bucket.data["snapshots"].bucket },
    { name = "CEDARS_BATCH_QUEUE_LARGE", value = aws_batch_job_queue.runs["large"].name },
    { name = "CEDARS_BATCH_QUEUE_SMALL", value = aws_batch_job_queue.runs["small"].name },
    { name = "CEDARS_BATCH_JOB_DEFINITION", value = aws_batch_job_definition.recet.name },
  ]
  web_container = {
    name                   = "web"
    image                  = var.web_image
    essential              = true
    portMappings           = [{ containerPort = 8000, protocol = "tcp" }]
    environment            = local.web_environment
    secrets                = local.app_secrets
    readonlyRootFilesystem = false
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "web"
      }
    }
  }
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${local.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.web.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([local.web_container])
  tags                  = local.tags
}

resource "aws_ecs_service" "web" {
  name                               = "web"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.web.arn
  desired_count                      = var.web_count
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.web.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  tags       = local.tags
  depends_on = [aws_lb_listener.https]
}

# --- load balancer

resource "aws_lb" "web" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.public[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
  idle_timeout               = 300 # uploads of up to 150 MB
  tags                       = local.tags
}

resource "aws_lb_target_group" "web" {
  name        = "${local.name}-web"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  deregistration_delay = 30
  tags                 = local.tags
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_wafv2_web_acl" "web" {
  name  = local.name
  scope = "REGIONAL"
  default_action {
    allow {}
  }
  # Any one address over waf_requests_per_5min is blocked until it slows down (the API also limits each user, in the app)
  rule {
    name     = "rate-per-address"
    priority = 0
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_requests_per_5min
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "rate-per-address"
      sampled_requests_enabled   = true
    }
  }
  dynamic "rule" {
    for_each = { AWSManagedRulesCommonRuleSet = 1, AWSManagedRulesKnownBadInputsRuleSet = 2, AWSManagedRulesSQLiRuleSet = 3 }
    content {
      name     = rule.key
      priority = rule.value
      override_action {
        none {}
      }
      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.key
          # uploads are large multipart bodies; the common set's body-size rule would block them
          dynamic "rule_action_override" {
            for_each = rule.key == "AWSManagedRulesCommonRuleSet" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }
      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.key
        sampled_requests_enabled   = true
      }
    }
  }
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.name
    sampled_requests_enabled   = true
  }
  tags = local.tags
}

resource "aws_wafv2_web_acl_association" "web" {
  resource_arn = aws_lb.web.arn
  web_acl_arn  = aws_wafv2_web_acl.web.arn
}
