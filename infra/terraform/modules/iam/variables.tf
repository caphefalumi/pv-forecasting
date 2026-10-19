variable "project_name" {
  type        = string
  description = "Project name prefix"
}

variable "github_oidc_sub_prefix" {
  type        = string
  description = "GitHub OIDC sub claim prefix (immutable subject form), e.g. repo:<owner>@<owner_id>/<repo>@<repo_id>"
}
