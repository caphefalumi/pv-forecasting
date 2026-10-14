output "alb_dns_name" {
  description = "Public DNS name of the ALB (Point Vercel HTTP requests here)"
  value       = "http://${module.alb.alb_dns_name}"
}

output "ecr_repository_url" {
  description = "ECR Repository URL for pushing FastAPI images"
  value       = module.ecr.backend_repository_url
}

output "pipeline_ecr_repository_url" {
  description = "ECR Repository URL for pushing scheduled data pipeline images"
  value       = module.ecr.pipeline_repository_url
}

output "ecs_cluster_name" {
  description = "ECS Cluster Name"
  value       = module.ecs.cluster_name
}

output "ecs_service_name" {
  description = "ECS Service Name"
  value       = module.ecs.service_name
}

output "github_actions_role_arn" {
  description = "IAM Role ARN to configure in GitHub Actions secret AWS_ROLE_ARN"
  value       = module.iam.github_actions_role_arn
}

output "daily_pipeline_schedule_arn" {
  description = "EventBridge Daily Ingest Pipeline Schedule ARN"
  value       = module.pipeline_scheduler.daily_schedule_arn
}

output "s3_data_lake_bucket" {
  description = "S3 Data Lake Bucket Name"
  value       = module.storage.bucket_name
}

output "s3_data_lake_uri" {
  description = "S3 URI for PV Data Lake"
  value       = module.storage.bucket_uri
}
