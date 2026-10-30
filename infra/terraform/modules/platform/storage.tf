# Object storage: uploads (submitted files), results (run folders and receipts), refdata (data packs, never overwritten),
# snapshots (publication snapshots, Parquet). Versioned, encrypted, never public. Retention is the application's job
# (decision M, the retention service); the lifecycle rules here only clear old non-current versions and failed uploads.

locals {
  buckets = {
    uploads   = { noncurrent_days = 90 }
    results   = { noncurrent_days = 30 }
    refdata   = { noncurrent_days = null } # a published pack's files never change; old versions are kept
    snapshots = { noncurrent_days = null }
  }
}

resource "aws_s3_bucket" "data" {
  for_each = local.buckets
  bucket   = "${local.name}-${each.key}-${data.aws_caller_identity.current.account_id}"
  tags     = merge(local.tags, { Purpose = each.key })
}

resource "aws_s3_bucket_versioning" "data" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.data.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  for_each                = aws_s3_bucket.data
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "data" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  for_each = aws_s3_bucket.data
  bucket   = each.value.id
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
  dynamic "rule" {
    for_each = local.buckets[each.key].noncurrent_days == null ? [] : [local.buckets[each.key].noncurrent_days]
    content {
      id     = "expire-noncurrent"
      status = "Enabled"
      filter {}
      noncurrent_version_expiration {
        noncurrent_days = rule.value
      }
    }
  }
}

resource "aws_ecr_repository" "images" {
  for_each             = toset(["cedars-web", "recet-worker"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE" # a pinned engine release always means the same image
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.data.arn
  }
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = local.tags
}
