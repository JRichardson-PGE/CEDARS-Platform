# The CEDARS platform in one AWS account and region (decision A: the CPUC's AWS). Written, not applied: the CPUC's cloud team
# reviews it against their landing zone (account, network, identity, logging) before any apply.

locals {
  name = "cedars-${var.environment}"
  tags = merge(var.tags, { Application = "CEDARS", Environment = var.environment, ManagedBy = "terraform" })
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# Encryption keys: one for the platform's data, one for the PII vault only (decision G), so access to one never implies the other.
resource "aws_kms_key" "data" {
  description             = "${local.name} data (database, object storage, logs, secrets)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_kms_key" "vault" {
  count                   = var.pii_vault ? 1 : 0
  description             = "${local.name} PII vault"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "vault" {
  count         = var.pii_vault ? 1 : 0
  name          = "alias/${local.name}-vault"
  target_key_id = aws_kms_key.vault[0].key_id
}
