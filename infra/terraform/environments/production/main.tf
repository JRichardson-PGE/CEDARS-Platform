# The production environment. Not applied: the CPUC's cloud team supplies the account, region, state bucket, certificate and
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
    tags = { Application = "CEDARS", Environment = "production" }
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

variable "alarm_emails" {
  description = "Who the CloudWatch alarms email"
  type        = list(string)
  default     = []
}

module "platform" {
  source = "../../modules/platform"

  environment         = "production"
  vpc_cidr            = "10.40.0.0/16"
  certificate_arn     = var.certificate_arn
  web_image           = var.web_image
  worker_image        = var.worker_image
  alarm_emails        = var.alarm_emails
  domain_name         = "cedars.example.invalid"
  nat_per_az          = true
  db_instance_class   = "db.m7g.large"
  db_allocated_gb     = 200
  db_multi_az         = true
  web_count           = 2
  batch_max_vcpus     = 64
  small_run_max_vcpus = 8
}

output "platform" {
  value = module.platform
}
