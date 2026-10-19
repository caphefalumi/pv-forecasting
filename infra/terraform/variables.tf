variable "aws_region" {
  type        = string
  description = "AWS region for deployment"
  default     = "ap-south-1"
}

variable "environment" {
  type        = string
  description = "Environment name (dev or prod)"
  default     = "prod"
}

variable "project_name" {
  type        = string
  description = "Project name prefix"
  default     = "pv-forecasting"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository (owner/repo) for OIDC authentication"
  default     = "COS40007-2026-MinhHA10-Fall26/pv-forecasting"
}

variable "supabase_url" {
  type        = string
  description = "Supabase API URL"
  default     = "https://lxtdcahxubojfipsetml.supabase.co/rest/v1/"
}

variable "supabase_service_role_key" {
  type        = string
  description = "Supabase Service Role Key (injected via GitHub Actions secrets)"
  sensitive   = true
  default     = ""
}

variable "mlflow_tracking_uri" {
  type        = string
  description = "MLflow tracking server URI or backend store URI (sensitive)"
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

variable "otel_exporter_otlp_headers" {
  type        = string
  description = "OTLP Authorization header from GitHub Actions env"
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
  description = "Grafana Cloud API token from GitHub Actions env"
  sensitive   = true
  default     = ""
}

variable "api_desired_count" {
  type        = number
  description = "FastAPI desired count (0 when not demoing)"
  default     = 0
}

variable "schedules_enabled" {
  type        = bool
  description = "Enable EventBridge pipeline schedules (false when not demoing)"
  default     = false
}
