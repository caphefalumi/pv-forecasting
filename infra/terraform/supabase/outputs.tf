output "project_id" {
  description = "Supabase Project Reference ID"
  value       = supabase_project.production.id
}

output "project_name" {
  description = "Supabase Project Name"
  value       = supabase_project.production.name
}

output "anon_key" {
  description = "Supabase Anon Key"
  value       = data.supabase_apikeys.production.anon_key
  sensitive   = true
}

output "service_role_key" {
  description = "Supabase Service Role Key"
  value       = data.supabase_apikeys.production.service_role_key
  sensitive   = true
}
