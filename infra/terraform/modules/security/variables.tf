variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID where security groups will be created"
}

variable "alb_ingress_ipv4_cidrs" {
  type        = list(string)
  description = "IPv4 CIDRs allowed to reach the ALB on 80/443 (Cloudflare edge ranges)"

  validation {
    condition     = length(var.alb_ingress_ipv4_cidrs) > 0 && !contains(var.alb_ingress_ipv4_cidrs, "0.0.0.0/0")
    error_message = "Provide the Cloudflare IPv4 ranges; an empty list or 0.0.0.0/0 is not allowed."
  }
}

variable "alb_ingress_ipv6_cidrs" {
  type        = list(string)
  description = "IPv6 CIDRs allowed to reach the ALB on 80/443 (Cloudflare edge ranges)"

  validation {
    condition     = !contains(var.alb_ingress_ipv6_cidrs, "::/0")
    error_message = "::/0 is not allowed; provide the Cloudflare IPv6 ranges."
  }
}