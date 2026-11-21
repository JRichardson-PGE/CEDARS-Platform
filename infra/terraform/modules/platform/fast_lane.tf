# The calculations sync and the fast lane: one always-on ECS service, two containers of the web image in one task.
# - "jobs": `calculations_sync --loop` every 10 seconds. It copies AWS Batch runs' and check jobs' progress and receipts into their
#   records, hands queued fast-lane work to Batch when no fast worker is ready, and keeps the workers' reference folder (efs.tf) filled
#   from the data packs (the only writer; it mounts the folder read-write).
# - "fast-lane": `calculations_worker --fast`, the always-on worker that takes check jobs and runs of up to the fast_lane_records limit
#   (5,000 records unless a CPUC system administrator changes it), so they start within seconds instead of waiting for Batch to start
#   a worker. It mounts the reference folder read-only, prices a one-measure job on each ACC before it says it is ready, and keeps a
#   ready file fresh while it is; its health check reads that file. Larger work, and anything sent while it is not ready, goes to Batch.
# A deploy starts the new task and waits for the fast lane to be ready (healthy) before stopping the old one, so small work never
# loses its fast start; the old worker finishes what it is doing when it is stopped (SIGTERM, then up to 120 seconds).

locals {
  sync_container = merge(local.jobs_container, {
    command     = ["python", "manage.py", "calculations_sync", "--loop", "--sleep", "10"]
    environment = concat(local.web_environment, [{ name = "CALCULATIONS_REFERENCE_FOLDER", value = local.reference_path }])
    mountPoints = [{ sourceVolume = "reference", containerPath = local.reference_path, readOnly = false }]
  })
  fast_lane_container = merge(local.jobs_container, {
    name    = "fast-lane"
    command = ["python", "manage.py", "calculations_worker", "--fast"]
    environment = concat(local.web_environment, [
      { name = "RECET_DATA", value = local.reference_path },
      { name = "CALCULATIONS_FAST_READY_FILE", value = "/tmp/cedars-fast-lane-ready" },
    ])
    mountPoints = [{ sourceVolume = "reference", containerPath = local.reference_path, readOnly = true }]
    stopTimeout = 120
    healthCheck = {
      command     = ["CMD-SHELL", "test -n \"$(find /tmp/cedars-fast-lane-ready -mmin -2 2>/dev/null)\""]
      interval    = 30
      timeout     = 5
      retries     = 10
      startPeriod = 300 # with the retries: up to 10 minutes to warm up on every ACC
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "fast-lane"
      }
    }
  })
}

resource "aws_ecs_task_definition" "calculations_sync" {
  family                   = "${local.name}-calculations-sync"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.fast_lane_cpu
  memory                   = var.fast_lane_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.web.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  ephemeral_storage {
    size_in_gib = var.fast_lane_disk_gb # the engine's cache of the ACC files and profiles it warmed up on, and the jobs' work folders
  }
  volume {
    name = "reference"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.reference.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.reference.id
        iam             = "ENABLED"
      }
    }
  }
  container_definitions = var.fast_lane ? jsonencode([local.sync_container, local.fast_lane_container]) : jsonencode([local.sync_container])
  tags                  = local.tags
}

resource "aws_ecs_service" "calculations_sync" {
  name            = "calculations-sync"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.calculations_sync.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  # the new task runs beside the old until it is healthy (the fast lane warmed up), so small work keeps its fast start through a
  # deploy; the two syncs overlap for those minutes, which they may (every record change they make is conditional)
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
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
  depends_on = [aws_efs_mount_target.reference]
  tags       = local.tags
}

moved {
  from = aws_ecs_task_definition.jobs["calculations-sync"]
  to   = aws_ecs_task_definition.calculations_sync
}

moved {
  from = aws_ecs_service.jobs["calculations-sync"]
  to   = aws_ecs_service.calculations_sync
}
