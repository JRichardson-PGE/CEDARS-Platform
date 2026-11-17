# The staging environment. Not applied: the CPUC's cloud team supplies the account, region, state bucket, certificate and
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
    tags = { Application = "CEDARS", Environment = "staging" }
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

variable "allowed_cidrs" {
  description = "CPUC and PA test networks"
  type        = list(string)
}
module "platform" {
  source = "../../modules/platform"

  environment         = "staging"
  vpc_cidr            = "10.42.0.0/16"
  certificate_arn     = var.certificate_arn
  web_image           = var.web_image
  worker_image        = var.worker_image
  alarm_emails        = var.alarm_emails
  domain_name         = "staging.cedars.example.invalid"
  allowed_cidrs       = var.allowed_cidrs # staging is not public
  db_instance_class   = "db.t4g.large"
  web_count           = 1
  batch_max_vcpus     = 16
  small_run_max_vcpus = 4
}

output "platform" {
  value = module.platform
}
