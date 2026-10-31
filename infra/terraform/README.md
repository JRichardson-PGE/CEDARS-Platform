# Infrastructure as code

The platform on the CPUC's AWS account (decision A), written in Terraform and **never applied from this repository**. The
CPUC's cloud team reviews it against their landing zone (accounts, network, identity, central logging, guardrails), supplies
the values below and runs `terraform plan` and `apply` from their own pipeline. CI checks only that it is formatted and valid.

## Layout

- `modules/platform/`: one environment's stack.
  - `network.tf`: VPC over two (or more) zones, public subnets for the load balancer only, private subnets for everything else, NAT, an S3 gateway endpoint, flow logs, and security groups. Only the web tasks reach the PII vault; calculation workers reach no database.
  - `data.tf`: PostgreSQL 16 on RDS (encrypted, TLS only, 35-day backups, Performance Insights). The PII vault is a separate instance with its own KMS key (decision G).
  - `storage.tf`: S3 buckets for uploads, results, data packs and snapshots (versioned, KMS, TLS only, never public), and ECR repositories with immutable tags.
  - `web.tf`: ECS Fargate web service behind an HTTPS load balancer (TLS 1.2+) with AWS WAF managed rules.
  - `jobs.tf`: the background work, in the web image with a management command. Two always-on ECS services, one task each: `profile-jobs` (`run_impact_profile_jobs --loop`: upload checks, profile generation, eTRM syncs started from the page, library writes; sized by `profile_jobs_cpu`, `profile_jobs_memory`, `profile_jobs_disk_gb`) and `calculations-sync` (`calculations_sync --loop`: Batch runs' progress, receipts and ends once a minute). On EventBridge Scheduler, Pacific time: `purge_expired` 02:15, `close_recertifications` 02:30, `documents_read_text` 03:45 (files stored without their text), `send_deadline_reminders` 07:00, and `sync_etrm` 01:00 once `etrm_database` is true. The services, `documents_read_text` and `sync_etrm` run in the `jobs` security group, which reaches the core database only, and are not given the PII vault's URL.
  - `workers.tf`: AWS Batch on Fargate for `recet worker run`, with a large-run queue and a small-run queue that has its own capacity.
  - `secrets.tf`: the application secret (one JSON value in Secrets Manager).
- `environments/{sandbox,staging,production}/`: each calls the module with its own sizes. The sandbox has no PII vault and no deletion protection. Staging is reachable only from `allowed_cidrs`. Production runs NAT per zone and Multi-AZ databases.

## What the CPUC supplies

| Value | Where |
| --- | --- |
| AWS account, region | provider (`region`, default us-west-2) |
| State bucket, lock table, KMS key | `backend.hcl` for `terraform init -backend-config=backend.hcl` |
| Domain name and its ACM certificate | `domain_name` (placeholder `*.example.invalid`), `certificate_arn` |
| Images | `web_image` (this repository's Dockerfile), `worker_image` (a ReCET release's worker image) |
| Application secret | after the first apply: `DJANGO_SECRET_KEY`, `DATABASE_URL`, `VAULT_DATABASE_URL` (not in the sandbox), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (SES SMTP); `ETRM_DATABASE_URL` once eTRM grants a read-only role, then set `etrm_database = true` |
| DNS | a record for `domain_name` pointing at the `load_balancer_dns` output |

## After the first apply

1. Create the application's database roles (not the RDS master) and put the URLs in the app secret.
2. Verify the sending domain in SES and create SMTP credentials.
3. Run the migrations as a one-off task: `aws ecs run-task` on the web task definition, with the command `python manage.py migrate`, then (not in the sandbox) `python manage.py migrate --database vault` for the PII vault's tables.
4. Create the first CPUC administrator: `python manage.py bootstrap_cpuc EMAIL --steward`. In the sandbox, also run `python manage.py seed_sandbox`.

## Check locally

```
cd infra/terraform/environments/sandbox
terraform init -backend=false && terraform validate
```
