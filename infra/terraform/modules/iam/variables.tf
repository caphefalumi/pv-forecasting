variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "github_repo" {
  type        = string
  description = "GitHub repository owner/name for OIDC"
}

variable "s3_data_lake_bucket_arn" {
  type        = string
  description = "ARN of S3 data lake bucket for task IAM permissions"
  default     = ""
}
