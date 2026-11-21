# Infrastructure as code

The platform on the CPUC's AWS account, written in Terraform and **never applied from this repository**. The
CPUC's cloud team reviews it against their landing zone (accounts, network, identity, central logging, guardrails), supplies
the values below and runs `terraform plan` and `apply` from their own pipeline. CI checks only that it is formatted and valid.

## Layout

- `modules/platform/`: one environment's stack.
  - `network.tf`: VPC over two (or more) zones, public subnets for the load balancer only, private subnets for everything else, NAT, an S3 gateway endpoint, flow logs, and security groups. Only the web tasks reach the PII vault; calculation workers reach no database.
  - `data.tf`: PostgreSQL 16 on RDS (encrypted, TLS only, 35-day backups, Performance Insights). The PII vault is a separate instance with its own KMS key.
  - `storage.tf`: S3 buckets for uploads, results, data packs and snapshots (versioned, KMS, TLS only, never public), and ECR repositories with immutable tags.
  - `web.tf`: ECS Fargate web service behind an HTTPS load balancer (TLS 1.2+) with AWS WAF managed rules.
  - `fast_lane.tf`: the `calculations-sync` ECS service, one task with two containers of the web image: `calculations_sync --loop --sleep 10` (Batch runs' and check jobs' progress, receipts and ends; queued checks; hands fast-lane work to Batch when the fast worker is not ready; fills the reference folder) and, with `fast_lane` (on by default), `calculations_worker --fast`, the always-on worker that starts check jobs and runs up to the editable `fast_lane_records` limit (5,000 records) within seconds. It warms up on each ACC before its health check passes; a deploy keeps the old task until the new one is healthy (minimum healthy 100%). Sized by `fast_lane_cpu` (1 vCPU), `fast_lane_memory` (4 GiB) and `fast_lane_disk_gb` (40): about $33 a month more than the sync alone.
  - `efs.tf`: the workers' shared reference folder (ACC files and impact-profile library, about 15 GB): an encrypted EFS file system with a mount target in each private subnet, one access point, TLS and IAM required, NFS from the jobs and worker security groups only. The calculations sync writes it; Batch ReCET jobs and the fast lane mount it read-only at `/mnt/reference` (`RECET_DATA`). `efs_throughput_mode` is `bursting` (reads free) with an alarm on low burst credits, or `elastic`.
  - `jobs.tf`: the background work, in the web image with a management command. Two always-on ECS services, one task each: `profile-jobs` (`run_impact_profile_jobs --loop`: upload checks, profile generation, eTRM syncs started from the page, library writes; sized by `profile_jobs_cpu`, `profile_jobs_memory`, `profile_jobs_disk_gb`) and `data-downloads` (`run_data_downloads --loop`: record downloads too large to make in a request). On EventBridge Scheduler, Pacific time: `purge_expired` 02:15, `close_recertifications` 02:30, `documents_read_text` 03:45 (files stored without their text), `send_deadline_reminders` 07:00, and `sync_etrm` 01:00 once `etrm_database` is true. The services, `documents_read_text` and `sync_etrm` run in the `jobs` security group, which reaches the core database only, and are not given the PII vault's URL.
  - `workers.tf`: AWS Batch on Fargate for `recet worker run`, with a large-run queue and a small-run queue that has its own capacity.
  - `monitoring.tf`: health monitoring. `manage.py check_health` every 10 minutes (it alerts the CPUC's system administrators in the app and prints one JSON line); log metric filters on that line, and CloudWatch alarms for failing checks, checks that stop reporting, unhealthy or missing web tasks, a background job service (`jobs.tf`) with no task running, 5xx answers, web memory, and database CPU and storage, all to one KMS-encrypted SNS topic that emails `alarm_emails`.
  - `secrets.tf`: the application secret (one JSON value in Secrets Manager).
- `environments/{sandbox,staging,production}/`: each calls the module with its own sizes. The sandbox has no PII vault and no deletion protection. Staging is reachable only from `allowed_cidrs`. Production runs NAT per zone and Multi-AZ databases.

## What the CPUC supplies

| Value | Where |
| --- | --- |
| AWS account, region | provider (`region`, default us-west-2) |
| State bucket, lock table, KMS key | `backend.hcl` for `terraform init -backend-config=backend.hcl` |
| Domain name and its ACM certificate | `domain_name` (placeholder `*.example.invalid`), `certificate_arn` |
| Alarm recipients | `alarm_emails` (each address confirms the SNS subscription) |
| Images | `web_image` (this repository's Dockerfile), `worker_image` (a ReCET release's worker image) |
| Application secret | after the first apply: `DJANGO_SECRET_KEY`, `DATABASE_URL`, `VAULT_DATABASE_URL` (not in the sandbox), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (SES SMTP); `ETRM_DATABASE_URL` once eTRM grants a read-only role, then set `etrm_database = true`; `CEDARS_ISSUE_WEBHOOK_SECRET` and `CEDARS_ISSUE_APP_PRIVATE_KEY` once the developers' tracker is connected, then set `issue_tracker = true` |
| Support and feedback | `support_email` (the address the sign-in pages and Help give), `feedback_url` (a form or survey; blank sends feedback by email to the support address) |
| Developers' tracker (optional) | `issue_repository` (owner/name, where a work item's issue is started), `issue_app_id` and `issue_app_installation_id` (for the site to post issues and read their events itself), with the two secrets above |
| DNS | a record for `domain_name` pointing at the `load_balancer_dns` output |

## After the first apply

1. Create the application's database roles (not the RDS master) and put the URLs in the app secret.
2. Verify the sending domain in SES and create SMTP credentials.
3. Run the migrations as a one-off task: `aws ecs run-task` on the web task definition, with the command `python manage.py migrate`, then (not in the sandbox) `python manage.py migrate --database vault` for the PII vault's tables.
4. Create the first CPUC administrator: `python manage.py bootstrap_cpuc EMAIL --steward`. In the sandbox, also run `python manage.py seed_sandbox`.

## Sandbox refresh (off by default)

Production writes a bundle of its published, non-PII data (`export_sandbox_bundle`) and the sandbox loads it (`refresh_sandbox`),
both in the jobs task, which has no PII vault URL. To schedule them, set in both environments' `main.tf`:

- `sandbox_bundle_uri`: the same `s3://bucket/prefix` in production and the sandbox (a bucket the CPUC's cloud team chooses);
- `sandbox_refresh_schedule`: production's export first (e.g. `cron(0 4 ? * SUN *)`), the sandbox's load a few hours later;
- in the sandbox, `sandbox_read_bucket_arns`: production's refdata bucket, which copied reference packs are read from in place.

The module grants its task role write (production) or read (sandbox) on the bundle's prefix. Where the bucket or production's refdata
bucket is in another account, that account's bucket policy and KMS key policy must also allow the other environment's task role. Run
by hand at any time: `python manage.py export_sandbox_bundle --to s3://...` in production, then `python manage.py refresh_sandbox
--from s3://...` in the sandbox; each refresh is listed in the audit log (`sandbox.refreshed`) with what it added and left out.

## Check locally

```
cd infra/terraform/environments/sandbox
terraform init -backend=false && terraform validate
```
