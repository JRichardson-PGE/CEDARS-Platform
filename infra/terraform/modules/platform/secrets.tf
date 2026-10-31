# The application's secret: one JSON value filled in by the CPUC's operators after the first apply, never by Terraform or a
# file in the repository. Keys: DJANGO_SECRET_KEY, DATABASE_URL, VAULT_DATABASE_URL (not in the sandbox), EMAIL_HOST_USER,
# EMAIL_HOST_PASSWORD, and ETRM_DATABASE_URL once eTRM grants a read-only role (then set etrm_database; only the jobs get it).

resource "aws_secretsmanager_secret" "app" {
  name       = "${local.name}/app"
  kms_key_id = aws_kms_key.data.arn
  tags       = local.tags
}

locals {
  secret_keys = concat(
    ["DJANGO_SECRET_KEY", "DATABASE_URL", "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD"],
    var.pii_vault ? ["VAULT_DATABASE_URL"] : [],
  )
  app_secrets = [for k in local.secret_keys : { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }]
}
