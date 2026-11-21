# The workers' shared reference folder: the ACC files and the impact-profile library the ReCET worker prices from. The calculations
# sync (fast_lane.tf) fills it from the data packs in the refdata bucket and writes its manifest.json (with each file's SHA-256, so a
# worker never hashes it), and every AWS Batch ReCET job (workers.tf) and the fast-lane worker mount it read-only at
# /mnt/reference (RECET_DATA). About 15 GB; reads are free in bursting throughput mode, and a CloudWatch alarm says when its burst
# credits run low (then switch efs_throughput_mode to "elastic", billed per GB read).

resource "aws_efs_file_system" "reference" {
  creation_token   = "${local.name}-reference"
  encrypted        = true
  kms_key_id       = aws_kms_key.data.arn
  performance_mode = "generalPurpose"
  throughput_mode  = var.efs_throughput_mode
  tags             = merge(local.tags, { Name = "${local.name}-reference" })
}

resource "aws_efs_mount_target" "reference" {
  count           = length(aws_subnet.private)
  file_system_id  = aws_efs_file_system.reference.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}

# Every client enters at /reference as the platform image's user (10001): the sync writes files that user owns, 0755 folders and 0644
# files, which the ReCET worker image's own user reads.
resource "aws_efs_access_point" "reference" {
  file_system_id = aws_efs_file_system.reference.id
  posix_user {
    uid = 10001
    gid = 10001
  }
  root_directory {
    path = "/reference"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0755"
    }
  }
  tags = local.tags
}

# Clients mount through the access point over TLS with their task role; only the calculations sync's role may write.
resource "aws_efs_file_system_policy" "reference" {
  file_system_id = aws_efs_file_system.reference.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = { AWS = "*" }
        Action    = "*"
        Resource  = aws_efs_file_system.reference.arn
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "ReadThroughTheAccessPoint"
        Effect    = "Allow"
        Principal = { AWS = [aws_iam_role.worker.arn, aws_iam_role.web.arn] }
        Action    = ["elasticfilesystem:ClientMount"]
        Resource  = aws_efs_file_system.reference.arn
        Condition = { StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.reference.arn } }
      },
      {
        Sid       = "SyncWrites"
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.web.arn }
        Action    = ["elasticfilesystem:ClientWrite"]
        Resource  = aws_efs_file_system.reference.arn
        Condition = { StringEquals = { "elasticfilesystem:AccessPointArn" = aws_efs_access_point.reference.arn } }
      },
    ]
  })
}

resource "aws_security_group" "efs" {
  name        = "${local.name}-efs"
  description = "The workers' reference folder (NFS from the jobs and the calculation workers only)"
  vpc_id      = aws_vpc.main.id
  tags        = local.tags
}

resource "aws_vpc_security_group_ingress_rule" "efs_from" {
  for_each                     = { jobs = aws_security_group.jobs.id, worker = aws_security_group.worker.id }
  security_group_id            = aws_security_group.efs.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  description                  = "NFS from the ${each.key} security group"
}

locals {
  # The task-definition volume and mount point every client uses (read-only except the calculations sync's).
  reference_volume = {
    name = "reference"
    efsVolumeConfiguration = {
      fileSystemId        = aws_efs_file_system.reference.id
      transitEncryption   = "ENABLED"
      authorizationConfig = { accessPointId = aws_efs_access_point.reference.id, iam = "ENABLED" }
    }
  }
  reference_path = "/mnt/reference"
}

resource "aws_cloudwatch_metric_alarm" "reference_burst_credits" {
  count               = var.efs_throughput_mode == "bursting" ? 1 : 0
  alarm_name          = "${local.name}-reference-folder-burst-credits"
  alarm_description   = "The workers' reference folder is running out of burst credits: runs will read it slowly. Set efs_throughput_mode to elastic."
  namespace           = "AWS/EFS"
  metric_name         = "BurstCreditBalance"
  dimensions          = { FileSystemId = aws_efs_file_system.reference.id }
  statistic           = "Minimum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 200 * 1024 * 1024 * 1024 # bytes: under 200 GiB of reads left at the burst rate
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
  tags                = local.tags
}
