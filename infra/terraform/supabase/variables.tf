variable "supabase_access_token" {
  type        = string
  description = "Supabase Personal Access Token (set via TF_VAR_supabase_access_token secret in CI, or falls back to access-token file locally)"
  default     = null
  sensitive   = true
}

variable "linked_project" {
  type        = string
  description = "Existing Supabase project reference"
  default     = "lxtdcahxubojfipsetml"
}

variable "organization_id" {
  type        = string
  description = "Supabase Organization ID"
  default     = "kfzmispeprgtfxhhhzal"
}

variable "project_name" {
  type        = string
  description = "Supabase Project Name"
  default     = "COS40007"
}

variable "database_password" {
  type        = string
  description = "Database master password (supplied via TF_VAR_database_password)"
  sensitive   = true
  default     = ""
}

variable "region" {
  type        = string
  description = "Supabase project AWS region"
  default     = "ap-south-1"
}
