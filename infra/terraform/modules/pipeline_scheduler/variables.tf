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

variable "otel_exporter_otlp_headers" {
  type        = string
  description = "OTLP Authorization header (Authorization=Basic ...)"
  sensitive   = true
  default     = ""
}

variable "grafana_otlp_instance_id" {
  type        = string
  description = "Grafana Cloud OTLP instance ID"
  default     = "1846261"
}

variable "grafana_cloud_api_token" {
  type        = string
  description = "Grafana Cloud API token for OTLP"
  sensitive   = true
  default     = ""
}

variable "schedules_enabled" {
  type        = bool
  description = "When false, EventBridge schedules stay DISABLED (scale-to-zero / non-demo)"
  default     = false
}
