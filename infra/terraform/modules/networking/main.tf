# Cost-saving mode: reuse the account default VPC (no custom VPC / IGW / NAT).
# ECS tasks get a public IP so they can reach Supabase, Open-Meteo, ECR, etc.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_subnet" "default" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  # Prefer subnets that auto-assign public IPs (typical default-VPC layout).
  public_like = [
    for id, subnet in data.aws_subnet.default : id
    if subnet.map_public_ip_on_launch
  ]
  subnet_ids = length(local.public_like) >= 2 ? local.public_like : sort(data.aws_subnets.default.ids)
}
