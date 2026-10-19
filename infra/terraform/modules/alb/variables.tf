variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "vpc_id" {
  type        = string
  description = "VPC ID"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "List of public subnet IDs (minimum 2)"
}

variable "alb_security_group_id" {
  type        = string
  description = "Security group ID for the ALB"
}

variable "health_check_path" {
  type        = string
  description = "Target group health check path"
  default     = "/health"
}

variable "enable_https" {
  type        = bool
  description = "Attach HTTPS listener and redirect HTTP→HTTPS (known at plan time)"
  default     = true
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for HTTPS listener"
  default     = ""
}
