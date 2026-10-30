output "load_balancer_dns" {
  description = "Point the site's DNS name at this (CNAME or alias)"
  value       = aws_lb.web.dns_name
}

output "app_secret_arn" {
  description = "Fill in the application's secret keys here after the first apply"
  value       = aws_secretsmanager_secret.app.arn
}

output "core_db_endpoint" {
  value = aws_db_instance.core.endpoint
}

output "vault_db_endpoint" {
  value = var.pii_vault ? aws_db_instance.vault[0].endpoint : null
}

output "buckets" {
  value = { for k, b in aws_s3_bucket.data : k => b.bucket }
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.images : k => r.repository_url }
}

output "batch_queues" {
  value = { for k, q in aws_batch_job_queue.runs : k => q.name }
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}
