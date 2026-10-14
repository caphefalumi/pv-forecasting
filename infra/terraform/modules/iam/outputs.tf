output "github_actions_role_arn" {
  description = "IAM Role ARN for GitHub Actions deployment"
  value       = aws_iam_role.github_actions.arn
}

output "ecs_execution_role_arn" {
  description = "IAM Role ARN for ECS Task Execution"
  value       = aws_iam_role.ecs_execution.arn
}

output "ecs_task_role_arn" {
  description = "IAM Role ARN for ECS Task"
  value       = aws_iam_role.ecs_task.arn
}

output "runtime_secret_arn" {
  description = "AWS Secrets Manager ARN for runtime secrets"
  value       = aws_secretsmanager_secret.runtime.arn
}
