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
  description = "Supabase S3 URI for PV data (e.g. s3://processed_data)"
  default     = "s3://processed_data"
}

variable "supabase_storage_bucket" {
  type        = string
  description = "Supabase Storage bucket name"
  default     = "processed_data"
}

variable "supabase_s3_endpoint" {
  type        = string
  description = "Supabase S3-compatible endpoint URL"
}

variable "supabase_s3_access_key" {
  type        = string
  description = "Supabase S3 access key"
  sensitive   = true
  default     = ""
}

variable "supabase_s3_secret_key" {
  type        = string
  description = "Supabase S3 secret key"
  sensitive   = true
  default     = ""
}

variable "otel_exporter_otlp_protocol" {
  type        = string
  description = "OTLP exporter protocol"
  default     = "http/protobuf"
}

variable "otel_exporter_otlp_endpoint" {
  type        = string
  description = "Grafana Cloud OTLP gateway URL"
  default     = "https://otlp-gateway-prod-ap-southeast-1.grafana.net/otlp"
}

variable "grafana_secrets" {
  type = list(object({
    name      = string
    valueFrom = string
  }))
  description = "ECS container secrets (SSM parameter ARNs) for the Grafana Cloud credentials"
  default     = []
}

variable "grafana_otlp_instance_id" {
  type        = string
  description = "Grafana Cloud OTLP instance ID"
  default     = "1846261"
}


variable "schedules_enabled" {
  type        = bool
  description = "When false, EventBridge schedules stay DISABLED (scale-to-zero / non-demo)"
  default     = false
}
