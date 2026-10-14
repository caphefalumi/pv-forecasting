terraform {
  required_version = ">= 1.5.0"
  required_providers {
    supabase = {
      source  = "supabase/supabase"
      version = "~> 1.0"
    }
  }

  backend "s3" {
    bucket                      = "terraform-state"
    key                         = "supabase/terraform.tfstate"
    region                      = "ap-south-1"
    endpoints                   = { s3 = "https://lxtdcahxubojfipsetml.supabase.co/storage/v1/s3" }
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
    use_path_style              = true
  }
}

provider "supabase" {
  access_token = coalesce(
    var.supabase_access_token,
    try(file("${path.module}/access-token"), null)
  )
}
