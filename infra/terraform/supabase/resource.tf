import {
  to = supabase_project.production
  id = var.linked_project
}

# Existing project resource imported into state
resource "supabase_project" "production" {
  organization_id   = var.organization_id
  name              = var.project_name
  database_password = var.database_password
  region            = var.region

  lifecycle {
    ignore_changes = [database_password]
  }
}

# Retrieve project API keys
data "supabase_apikeys" "production" {
  project_ref = supabase_project.production.id
}
