# Background work, all in the web image with a management command:
# - three always-on ECS services: the Impact profiles job worker (upload checks, profile generation, eTRM syncs started from the
#   page, library writes), the calculations sync (AWS Batch runs' progress, receipts and ends, once a minute) and the record
#   download worker (downloads too large to make in a request);
# - jobs on EventBridge Scheduler, Pacific time: the three daily ones, the nightly eTRM sync (once eTRM's database is reachable)
#   and the catch-up that reads the text of document files stored without it.
# The services and the jobs that need no personal information run in the "jobs" security group, which reaches the core database
# only, and are given no PII vault URL; purge_expired and the account jobs stay with the web tasks' access.

# --- the "jobs" task definition: the web container without its port, under its own log prefix

locals {
  jobs_secrets = concat(
    [for s in local.app_secrets : s if s.name != "VAULT_DATABASE_URL"],
    var.etrm_database ? [{ name = "ETRM_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.app.arn}:ETRM_DATABASE_URL::" }] : [],
  )
  jobs_container = merge(local.web_container, {
    name         = "jobs"
    portMappings = []
    secrets      = local.jobs_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "jobs"
      }
    }
  })

  # Always-on workers. Each runs one process; the Impact profiles worker writes the whole library (about 10 GB of Parquet) to its
  # scratch disk when it exports, hence its storage.
  job_services = {
    profile-jobs = {
      command = ["python", "manage.py", "run_impact_profile_jobs", "--loop", "--sleep", "5"]
      cpu     = var.profile_jobs_cpu
      memory  = var.profile_jobs_memory
      disk    = var.profile_jobs_disk_gb
    }
    calculations-sync = {
      command = ["python", "manage.py", "calculations_sync", "--loop", "--sleep", "60"]
      cpu     = 256
      memory  = 1024
      disk    = 21
    }
    # Record downloads too large to make in a request (BR-07.2): written to scratch disk, then stored in the results bucket.
    data-downloads = {
      command = ["python", "manage.py", "run_data_downloads", "--loop", "--sleep", "10"]
      cpu     = 512
      memory  = 2048
      disk    = 50
    }
  }

  # Scheduled jobs. `task` is the task definition and security group they run with.
  scheduled_jobs = merge(
    {
      purge-expired          = { command = ["python", "manage.py", "purge_expired"], at = "cron(15 2 * * ? *)", task = "web" }
      deadline-reminders     = { command = ["python", "manage.py", "send_deadline_reminders"], at = "cron(0 7 * * ? *)", task = "web" }
      close-recertifications = { command = ["python", "manage.py", "close_recertifications"], at = "cron(30 2 * * ? *)", task = "web" }
      documents-read-text    = { command = ["python", "manage.py", "documents_read_text"], at = "cron(45 3 * * ? *)", task = "jobs" }
    },
    # BR-11.3: nightly, before the working day, as CEDARS' Load Shape Tool pulls it. Only once the CPUC holds eTRM's read-only
    # database URL (the ETRM_DATABASE_URL key of the app secret); a sync with no source would fail every night.
    var.etrm_database ? {
      sync-etrm = { command = ["python", "manage.py", "sync_etrm"], at = "cron(0 1 * * ? *)", task = "jobs" }
    } : {},
  )
}

resource "aws_security_group" "jobs" {
  name        = "${local.name}-jobs"
  description = "Background services and scheduled jobs (core database only, never the PII vault)"
  vpc_id      = aws_vpc.main.id
  tags        = local.tags
}

resource "aws_vpc_security_group_egress_rule" "jobs_out" {
  security_group_id = aws_security_group.jobs.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "AWS APIs, email, the eTRM database"
}

resource "aws_vpc_security_group_ingress_rule" "db_from_jobs" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.jobs.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_ecs_task_definition" "jobs" {
  for_each                 = local.job_services
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.web.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  ephemeral_storage {
    size_in_gib = each.value.disk
  }
  container_definitions = jsonencode([merge(local.jobs_container, { command = each.value.command })])
  tags                  = local.tags
}

# The scheduled jobs' task definition (the same container; the schedule overrides its command)
resource "aws_ecs_task_definition" "scheduled" {
  family                   = "${local.name}-scheduled"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.web.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([local.jobs_container])
  tags                  = local.tags
}

resource "aws_ecs_service" "jobs" {
  for_each        = local.job_services
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.jobs[each.key].arn
  desired_count   = 1
  launch_type     = "FARGATE"
  # one process at a time: stop the old task before the new one starts (a job in progress is left running and is reported stalled
  # on the jobs page if the deploy cut it short)
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  enable_execute_command             = false
  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.jobs.id]
    assign_public_ip = false
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  tags = local.tags
}

# --- schedules

resource "aws_iam_role" "scheduler" {
  name = "${local.name}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = [
          "${aws_ecs_task_definition.web.arn_without_revision}:*",
          "${aws_ecs_task_definition.scheduled.arn_without_revision}:*",
        ]
      },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.web.arn] },
    ]
  })
}

resource "aws_scheduler_schedule" "daily" {
  for_each                     = local.scheduled_jobs
  name                         = "${local.name}-${each.key}"
  schedule_expression          = each.value.at
  schedule_expression_timezone = "America/Los_Angeles"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn
    ecs_parameters {
      task_definition_arn = each.value.task == "web" ? aws_ecs_task_definition.web.arn : aws_ecs_task_definition.scheduled.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [each.value.task == "web" ? aws_security_group.web.id : aws_security_group.jobs.id]
        assign_public_ip = false
      }
    }
    input = jsonencode({ containerOverrides = [{ name = each.value.task, command = each.value.command }] })
    retry_policy {
      maximum_retry_attempts = 2 # when the task could not be started; a job that starts and fails is the app's to report
    }
  }
}
