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
  description = "Supabase Service Role Key (injected via CI secrets or Secrets Manager)"
  sensitive   = true
  default     = ""
}

variable "mlflow_tracking_uri" {
  type        = string
  description = "MLflow tracking server URI or backend store URI (sensitive)"
  sensitive   = true
  default     = ""
}
