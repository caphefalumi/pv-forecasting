output "backend_repository_url" {
  description = "ECR Repository URL for backend prediction container"
  value       = aws_ecr_repository.backend.repository_url
}

output "pipeline_repository_url" {
  description = "ECR Repository URL for scheduled pipeline container"
  value       = aws_ecr_repository.pipeline.repository_url
}

output "mlflow_repository_url" {
  description = "ECR Repository URL for the MLflow tracking server container"
  value       = aws_ecr_repository.mlflow.repository_url
}

output "repository_url" {
  description = "Default repository URL (backward compatibility for backend)"
  value       = aws_ecr_repository.backend.repository_url
}
