output "uami_id" {
  value = azurerm_user_assigned_identity.o11y.id
}

output "uami_client_id" {
  value = azurerm_user_assigned_identity.o11y.client_id
}

output "uami_principal_id" {
  value = azurerm_user_assigned_identity.o11y.principal_id
}

output "role_assignments" {
  value = { for id, a in local.assignments : id => "${a.role} on ${a.scope}" }
}

output "skipped_requirements" {
  description = "Rows from identity/role-requirements.yaml not granted because a scope input was empty."
  value       = [for r in local.requirements : r.id if !contains(keys(local.resolvable), r.id)]
}
