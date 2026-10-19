variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "github_oidc_sub_prefix" {
  type        = string
  description = "GitHub OIDC sub claim prefix (immutable subject form), e.g. repo:<owner>@<owner_id>/<repo>@<repo_id>"
}

variable "grafana_aws_account_id" {
  type        = string
  description = "Grafana Cloud's AWS account that assumes the CloudWatch read role"
  default     = "008923505280"
}

variable "grafana_external_id" {
  type        = string
  description = "External ID for the Grafana CloudWatch role (the Grafana Cloud stack ID)"
  default     = "3616746"
}