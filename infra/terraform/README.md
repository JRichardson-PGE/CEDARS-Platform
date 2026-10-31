# Infrastructure as code

The platform on the CPUC's AWS account (decision A), written in Terraform and **never applied from this repository**. The
CPUC's cloud team reviews it against their landing zone (accounts, network, identity, central logging, guardrails), supplies
the values below and runs `terraform plan` and `apply` from their own pipeline. CI checks only that it is formatted and valid.

## Layout

- `modules/platform/`: one environment's stack.
  - `network.tf`: VPC over two (or more) zones, public subnets for the load balancer only, private subnets for everything else, NAT, an S3 gateway endpoint, flow logs, and security groups. Only the web tasks reach the PII vault; calculation workers reach no database.
  - `data.tf`: PostgreSQL 16 on RDS (encrypted, TLS only, 35-day backups, Performance Insights). The PII vault is a separate instance with its own KMS key (decision G).
  - `storage.tf`: S3 buckets for uploads, results, data packs and snapshots (versioned, KMS, TLS only, never public), and ECR repositories with immutable tags.
  - `web.tf`: ECS Fargate web service behind an HTTPS load balancer (TLS 1.2+) with AWS WAF managed rules, and the daily jobs (`purge_expired`, `send_deadline_reminders`, `close_recertifications`) on EventBridge Scheduler.
  - `workers.tf`: AWS Batch on Fargate for `recet worker run`, with a large-run queue and a small-run queue that has its own capacity.
  - `monitoring.tf`: health monitoring (BR-12.2). `manage.py check_health` every 10 minutes (it alerts the CPUC's system administrators in the app and prints one JSON line); log metric filters on that line, and CloudWatch alarms for failing checks, checks that stop reporting, unhealthy or missing web tasks, 5xx answers, web memory, and database CPU and storage, all to one KMS-encrypted SNS topic that emails `alarm_emails`.
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
| Application secret | after the first apply: `DJANGO_SECRET_KEY`, `DATABASE_URL`, `VAULT_DATABASE_URL` (not in the sandbox), `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (SES SMTP) |
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
