variable "environment" {
  description = "sandbox, staging or production"
  type        = string
  validation {
    condition     = contains(["sandbox", "staging", "production"], var.environment)
    error_message = "environment must be sandbox, staging or production."
  }
}

variable "domain_name" {
  description = "The site's host name, e.g. cedars.cpuc.ca.gov (DNS is the CPUC's)"
  type        = string
}

variable "certificate_arn" {
  description = "ACM certificate for domain_name, issued in the CPUC account"
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "availability_zones" {
  type    = number
  default = 2
}

variable "nat_per_az" {
  description = "One NAT gateway per zone (production) or one shared (sandbox, staging)"
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  description = "Who may reach the load balancer; 0.0.0.0/0 for the public site"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "waf_requests_per_5min" {
  description = "Requests one address may make in 5 minutes before the WAF blocks it (an agency network behind one address counts as one)"
  type        = number
  default     = 3000
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_gb" {
  type    = number
  default = 100
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "pii_vault" {
  description = "Create the separate PII vault database (never in the sandbox, which holds no PII)"
  type        = bool
  default     = true
}

variable "web_image" {
  description = "cedars-web image URI with its digest or tag"
  type        = string
}

variable "worker_image" {
  description = "recet-worker image URI (a pinned engine release)"
  type        = string
}

variable "web_cpu" {
  type    = number
  default = 1024
}

variable "web_memory" {
  type    = number
  default = 2048
}

variable "web_count" {
  type    = number
  default = 2
}

variable "profile_jobs_cpu" {
  description = "CPU units for the always-on Impact profiles job worker"
  type        = number
  default     = 1024
}

variable "profile_jobs_memory" {
  description = "MiB for the Impact profiles job worker (profile generation and the library export)"
  type        = number
  default     = 4096
}

variable "profile_jobs_disk_gb" {
  description = "Scratch disk for the Impact profiles job worker: the library export writes about 10 GB before uploading it"
  type        = number
  default     = 40
}

variable "etrm_database" {
  description = "eTRM's read-only database URL is in the app secret (ETRM_DATABASE_URL): pass it to the jobs and sync nightly (BR-11.3)"
  type        = bool
  default     = false
}

variable "worker_vcpu" {
  description = "vCPU per calculation job (Fargate)"
  type        = number
  default     = 2
}

variable "worker_memory" {
  description = "MiB per calculation job; a 1,000,000-record run peaks near 2.3 GiB"
  type        = number
  default     = 8192
}

variable "batch_max_vcpus" {
  description = "Most vCPUs the large-run queue may use at once"
  type        = number
  default     = 32
}

variable "small_run_max_vcpus" {
  description = "vCPUs reserved for the small-run queue, so small runs finish within a minute during a maximum job"
  type        = number
  default     = 8
}

variable "log_retention_days" {
  description = "Decision M: application logs 1 year"
  type        = number
  default     = 365
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
