# Health monitoring (BR-12.2): the application's own checks every 10 minutes (`manage.py check_health`, which also alerts the CPUC's
# system administrators in the app), and CloudWatch alarms that email the hosting team through one SNS topic (`alarm_emails`):
# - the app's checks failing (stalled or failed runs and Impact profiles jobs, the eTRM sync), from its JSON line in the app log;
# - the checks not reporting at all (the scheduler, the task or the database is down);
# - the web service: unhealthy targets, 5xx answers, too few tasks, memory;
# - the background job services (Impact profiles jobs, calculations sync): no task running;
# - the databases: CPU, free storage.
# Nothing in an alarm message is personal information.

locals {
  metric_namespace = "CEDARS/${var.environment}"
  databases = merge(
    { core = { id = aws_db_instance.core.identifier, gb = aws_db_instance.core.allocated_storage } },
    var.pii_vault ? { vault = { id = aws_db_instance.vault[0].identifier, gb = aws_db_instance.vault[0].allocated_storage } } : {},
  )
}

# --- where alarms go

resource "aws_kms_key" "alarms" {
  description         = "${local.name} alarm notifications"
  enable_key_rotation = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Account"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchPublishes"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
      },
    ]
  })
  tags = local.tags
}

resource "aws_sns_topic" "alarms" {
  name              = "${local.name}-alarms"
  kms_master_key_id = aws_kms_key.alarms.arn
  tags              = local.tags
}

resource "aws_sns_topic_subscription" "alarm_email" {
  for_each  = toset(var.alarm_emails)
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

# --- the application's checks, every 10 minutes

resource "aws_scheduler_schedule" "health" {
  name                         = "${local.name}-check-health"
  schedule_expression          = "rate(10 minutes)"
  schedule_expression_timezone = "America/Los_Angeles"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.web.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.web.id]
        assign_public_ip = false
      }
    }
    input = jsonencode({ containerOverrides = [{ name = "web", command = ["python", "manage.py", "check_health"] }] })
    retry_policy {
      maximum_retry_attempts = 1
    }
  }
}

# check_health prints one JSON line: {"event": "cedars_health", "failing": n, "attention": n, "checks": {...}}
resource "aws_cloudwatch_log_metric_filter" "health_failing" {
  name           = "${local.name}-health-failing"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"cedars_health\" }"
  metric_transformation {
    name      = "HealthChecksFailing"
    namespace = local.metric_namespace
    value     = "$.failing"
  }
}

resource "aws_cloudwatch_log_metric_filter" "health_reported" {
  name           = "${local.name}-health-reported"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"cedars_health\" }"
  metric_transformation {
    name      = "HealthCheckRuns"
    namespace = local.metric_namespace
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "health_failing" {
  alarm_name          = "${local.name}-health-checks-failing"
  alarm_description   = "A CEDARS health check is failing (stalled or failed jobs, the eTRM sync). Details: /admin/health/ on the site."
  namespace           = local.metric_namespace
  metric_name         = "HealthChecksFailing"
  statistic           = "Maximum"
  period              = 900
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching" # a missing report is the next alarm's
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "health_silent" {
  alarm_name          = "${local.name}-health-checks-silent"
  alarm_description   = "The CEDARS health checks have not reported for 30 minutes: the scheduler, the task or the database may be down."
  namespace           = local.metric_namespace
  metric_name         = "HealthCheckRuns"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

# --- the web service

resource "aws_cloudwatch_metric_alarm" "web_unhealthy" {
  alarm_name          = "${local.name}-web-unhealthy-targets"
  alarm_description   = "A web task fails the load balancer's /healthz check (the process or the database)."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.web.arn_suffix, TargetGroup = aws_lb_target_group.web.arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "web_5xx" {
  alarm_name          = "${local.name}-web-5xx"
  alarm_description   = "The web tasks answered more than ${var.alarm_5xx_per_5min} requests with a server error in each of two 5-minute periods."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  dimensions          = { LoadBalancer = aws_lb.web.arn_suffix }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.alarm_5xx_per_5min
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "web_tasks" {
  alarm_name          = "${local.name}-web-tasks"
  alarm_description   = "Fewer web tasks are running than the service asks for."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.web.name }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = var.web_count
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

# --- the background job services (jobs.tf): each runs one task all the time; a task that keeps crashing leaves none running
resource "aws_cloudwatch_metric_alarm" "job_tasks" {
  for_each            = aws_ecs_service.jobs
  alarm_name          = "${local.name}-${each.key}-tasks"
  alarm_description   = "The ${each.key} service has no task running for 10 minutes (it keeps stopping or cannot start)."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = each.value.name }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  comparison_operator = "LessThanThreshold"
  threshold           = each.value.desired_count
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "web_memory" {
  alarm_name          = "${local.name}-web-memory"
  alarm_description   = "The web tasks use more than 85% of their memory (dashboards and downloads run DuckDB in them)."
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  dimensions          = { ClusterName = aws_ecs_cluster.main.name, ServiceName = aws_ecs_service.web.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 85
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

# --- the databases

resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  for_each            = local.databases
  alarm_name          = "${local.name}-${each.key}-db-cpu"
  alarm_description   = "The ${each.key} database's CPU is above 80% for 15 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = each.value.id }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 80
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  for_each            = local.databases
  alarm_name          = "${local.name}-${each.key}-db-storage"
  alarm_description   = "The ${each.key} database has less than 10% of its storage free."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = each.value.id }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = each.value.gb * 1024 * 1024 * 1024 * 0.1 # of what it started with; storage also grows by itself
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}
