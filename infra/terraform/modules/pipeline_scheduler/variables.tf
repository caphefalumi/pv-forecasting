variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "cluster_arn" {
  type        = string
  description = "ARN of ECS cluster"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "List of private subnet IDs"
}

variable "ecs_security_group_id" {
  type        = string
  description = "Security Group ID for ECS tasks"
}

variable "pipeline_ecr_repository_url" {
  type        = string
  description = "ECR repository URL for scheduled pipeline container"
}

variable "ecs_execution_role_arn" {
  type        = string
  description = "ARN of ECS Execution Role"
}

variable "ecs_task_role_arn" {
  type        = string
  description = "ARN of ECS Task Role"
}

variable "supabase_url" {
  type        = string
  description = "Supabase API URL"
}

variable "supabase_service_role_key" {
  type        = string
  description = "Supabase Service Role Key"
  sensitive   = true
}

variable "pv_s3_uri" {
  type        = string
  description = "S3 URI for PV data lake synchronization"
  default     = "s3://pv-forecasting-data/lake"
}
