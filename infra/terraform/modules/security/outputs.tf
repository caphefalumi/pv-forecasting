output "alb_security_group_id" {
  description = "Security Group ID of the ALB"
  value       = aws_security_group.alb.id
}

output "ecs_security_group_id" {
  description = "Security Group ID of the ECS Fargate tasks"
  value       = aws_security_group.ecs.id
}
