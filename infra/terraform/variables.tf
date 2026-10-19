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

variable "github_oidc_sub_prefix" {
  type        = string
  description = "GitHub OIDC sub claim prefix. The repo uses immutable subjects (gh api repos/<owner>/<repo>/actions/oidc/customization/sub -> sub_claim_prefix)."
  default     = "repo:COS40007-2026-MinhHA10-Fall26@330655750/pv-forecasting@1398967244"
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

variable "supabase_storage_bucket" {
  type        = string
  description = "Supabase Storage bucket used as the data lake (S3-compatible)"
  default     = "processed_data"
}

variable "supabase_s3_endpoint" {
  type        = string
  description = "Supabase Storage S3-compatible endpoint"
  default     = "https://lxtdcahxubojfipsetml.supabase.co/storage/v1/s3"
}

variable "supabase_s3_access_key" {
  type        = string
  description = "Supabase S3 access key (same as Terraform backend)"
  sensitive   = true
  default     = ""
}

variable "supabase_s3_secret_key" {
  type        = string
  description = "Supabase S3 secret key (same as Terraform backend)"
  sensitive   = true
  default     = ""
}

variable "cors_origins" {
  type        = list(string)
  description = "Allowed browser origins for the FastAPI CORS middleware"
  default = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://pv-forecasting.vercel.app",
    "https://pv-forecasting-caphefalumis-projects.vercel.app",
  ]
}

variable "frontend_origin" {
  type        = string
  description = "Primary production frontend origin (Vercel)"
  default     = "https://pv-forecasting.vercel.app"
}

variable "api_domain" {
  type        = string
  description = "Public API hostname (Cloudflare → ALB)"
  default     = "api.tm-project.id.vn"
}

variable "cloudflare_zone_name" {
  type        = string
  description = "Cloudflare zone that owns api_domain"
  default     = "tm-project.id.vn"
}

variable "cloudflare_api_token" {
  type        = string
  description = "Cloudflare API token with Zone.DNS Edit + Zone.Settings Edit on tm-project.id.vn"
  sensitive   = true
  default     = ""
}

variable "cloudflare_zone_id" {
  type        = string
  description = "Cloudflare Zone ID for tm-project.id.vn (Dashboard → Overview). Skips zone name lookup when set."
  default     = "d57ab9087f88ee90bd2bde21e86a83f4"
}
