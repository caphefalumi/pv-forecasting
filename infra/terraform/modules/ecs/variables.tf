variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "List of private subnet IDs"
}

variable "ecs_security_group_id" {
  type        = string
  description = "Security Group ID for ECS tasks"
}

variable "target_group_arn" {
  type        = string
  description = "ARN of ALB Target Group"
}

variable "ecr_repository_url" {
  type        = string
  description = "ECR repository URL for backend container image"
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

variable "mlflow_tracking_uri" {
  type        = string
  description = "MLflow Tracking URI / Backend Store URI"
  sensitive   = true
}

variable "s3_bucket_uri" {
  type        = string
  description = "S3 URI for data lake and MLflow artifact store"
  default     = "s3://pv-forecasting-data-lake"
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

variable "api_desired_count" {
  type        = number
  description = "FastAPI desired count (0 = scaled down when not demoing)"
  default     = 0
}
