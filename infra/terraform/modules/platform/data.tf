# PostgreSQL 16 (the platform relies on its triggers and ranges). The master password is managed by RDS in Secrets Manager;
# the application's own login and DATABASE_URL go into the app secret (secrets.tf) after a one-time role setup.

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.private[*].id
  tags       = local.tags
}

resource "aws_db_parameter_group" "pg16" {
  name   = "${local.name}-pg16"
  family = "postgres16"
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "2000"
  }
  tags = local.tags
}

resource "aws_db_instance" "core" {
  identifier                      = "${local.name}-core"
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = var.db_instance_class
  allocated_storage               = var.db_allocated_gb
  max_allocated_storage           = var.db_allocated_gb * 4
  storage_type                    = "gp3"
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.data.arn
  db_name                         = "cedars"
  username                        = "cedars_admin"
  manage_master_user_password     = true
  master_user_secret_kms_key_id   = aws_kms_key.data.arn
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  parameter_group_name            = aws_db_parameter_group.pg16.name
  multi_az                        = var.db_multi_az
  backup_retention_period         = 35
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${local.name}-core-final"
  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.data.arn
  enabled_cloudwatch_logs_exports = ["postgresql"]
  auto_minor_version_upgrade      = true
  tags                            = local.tags
}

# The PII vault: its own instance, key and security group (decision G). Only PII_Contact and PII_Site live here.
resource "aws_db_instance" "vault" {
  count                         = var.pii_vault ? 1 : 0
  identifier                    = "${local.name}-vault"
  engine                        = "postgres"
  engine_version                = "16"
  instance_class                = "db.t4g.small"
  allocated_storage             = 20
  max_allocated_storage         = 100
  storage_type                  = "gp3"
  storage_encrypted             = true
  kms_key_id                    = aws_kms_key.vault[0].arn
  db_name                       = "vault"
  username                      = "vault_admin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.vault[0].arn
  db_subnet_group_name          = aws_db_subnet_group.main.name
  vpc_security_group_ids        = [aws_security_group.vault[0].id]
  parameter_group_name          = aws_db_parameter_group.pg16.name
  multi_az                      = var.db_multi_az
  backup_retention_period       = 35
  copy_tags_to_snapshot         = true
  deletion_protection           = var.deletion_protection
  skip_final_snapshot           = false
  final_snapshot_identifier     = "${local.name}-vault-final"
  auto_minor_version_upgrade    = true
  tags                          = merge(local.tags, { DataClass = "PII" })
}
