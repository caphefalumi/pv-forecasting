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

output "grafana_cloudwatch_role_arn" {
  description = "IAM Role ARN Grafana Cloud assumes to read CloudWatch"
  value       = aws_iam_role.grafana_cloudwatch.arn
}