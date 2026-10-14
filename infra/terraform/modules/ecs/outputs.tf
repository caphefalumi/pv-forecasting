output "cluster_name" {
  description = "ECS Cluster Name"
  value       = aws_ecs_cluster.main.name
}

output "cluster_arn" {
  description = "ECS Cluster ARN"
  value       = aws_ecs_cluster.main.arn
}

output "service_name" {
  description = "FastAPI ECS Service Name"
  value       = aws_ecs_service.fastapi.name
}

output "mlflow_service_name" {
  description = "MLflow ECS Service Name"
  value       = aws_ecs_service.mlflow.name
}
