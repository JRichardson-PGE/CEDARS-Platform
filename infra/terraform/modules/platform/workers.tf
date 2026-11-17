# ReCET calculation jobs on AWS Batch (Fargate): `recet worker run job.json` with a pinned engine image. Two queues share the
# compute: "large" for full runs and "small" with its own reserved capacity, so a small run finishes within a minute while a
# 2,000,000-record job is running. Workers read inputs and data packs from S3 and write the run folder and receipt.json back;
# they never reach a database.

resource "aws_iam_role" "batch_service" {
  name = "${local.name}-batch-service"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "batch.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "batch_service" {
  role       = aws_iam_role.batch_service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBatchServiceRole"
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.data["uploads"].arn, "${aws_s3_bucket.data["uploads"].arn}/*",
          aws_s3_bucket.data["refdata"].arn, "${aws_s3_bucket.data["refdata"].arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:AbortMultipartUpload"]
        Resource = [aws_s3_bucket.data["results"].arn, "${aws_s3_bucket.data["results"].arn}/*"]
      },
      { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.data.arn },
    ]
  })
}

resource "aws_batch_compute_environment" "runs" {
  for_each                 = { large = var.batch_max_vcpus, small = var.small_run_max_vcpus }
  compute_environment_name = "${local.name}-${each.key}"
  type                     = "MANAGED"
  service_role             = aws_iam_role.batch_service.arn
  compute_resources {
    type               = "FARGATE"
    max_vcpus          = each.value
    subnets            = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.worker.id]
  }
  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.batch_service]
}

resource "aws_batch_job_queue" "runs" {
  for_each = { large = 10, small = 20 }
  name     = "${local.name}-${each.key}"
  state    = "ENABLED"
  priority = each.value
  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.runs[each.key].arn
  }
  tags = local.tags
}

resource "aws_batch_job_definition" "recet" {
  name                  = "${local.name}-recet"
  type                  = "container"
  platform_capabilities = ["FARGATE"]
  propagate_tags        = true
  retry_strategy {
    attempts = 1
  }
  timeout {
    attempt_duration_seconds = 3600
  }
  container_properties = jsonencode({
    image            = var.worker_image
    command          = ["recet", "worker", "run", "Ref::job_spec"]
    jobRoleArn       = aws_iam_role.worker.arn
    executionRoleArn = aws_iam_role.execution.arn
    resourceRequirements = [
      { type = "VCPU", value = tostring(var.worker_vcpu) },
      { type = "MEMORY", value = tostring(var.worker_memory) },
    ]
    ephemeralStorage             = { sizeInGiB = 60 } # a 2,000,000-record run's parts folder and outputs
    networkConfiguration         = { assignPublicIp = "DISABLED" }
    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = "worker"
      }
    }
  })
  tags = local.tags
}
