# The sandbox environment. Not applied: the CPUC's cloud team supplies the account, region, state bucket, certificate and
# domain, and reviews the plan first (infra/terraform/README.md).

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
  # Partial configuration: terraform init -backend-config=backend.hcl (bucket, key, region, dynamodb_table, kms_key_id)
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Application = "CEDARS", Environment = "sandbox" }
  }
}

variable "region" {
  type    = string
  default = "us-west-2"
}

variable "certificate_arn" {
  type = string
}

variable "web_image" {
  type = string
}

variable "worker_image" {
  type = string
}

module "platform" {
  source = "../../modules/platform"

  environment         = "sandbox"
  vpc_cidr            = "10.41.0.0/16"
  certificate_arn     = var.certificate_arn
  web_image           = var.web_image
  worker_image        = var.worker_image
  domain_name         = "sandbox.cedars.example.invalid" # the CPUC sets this
  pii_vault           = false                            # the sandbox holds no PII
  db_instance_class   = "db.t4g.medium"
  web_count           = 1
  batch_max_vcpus     = 16
  small_run_max_vcpus = 4
  deletion_protection = false
}

output "platform" {
  value = module.platform
}
