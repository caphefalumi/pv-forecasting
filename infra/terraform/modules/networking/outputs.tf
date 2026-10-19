output "vpc_id" {
  description = "Default VPC ID (no custom VPC created)"
  value       = data.aws_vpc.default.id
}

output "public_subnet_ids" {
  description = "Default VPC subnet IDs used by ALB and ECS (public IP enabled)"
  value       = local.subnet_ids
}

# Kept for module callers; same as public subnets (no private/NAT tier).
output "private_subnet_ids" {
  description = "Subnet IDs for ECS tasks (public subnets with assign_public_ip)"
  value       = local.subnet_ids
}
