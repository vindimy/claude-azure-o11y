output "function_app_name" {
  value = azurerm_linux_function_app.this.name
}

output "function_app_id" {
  value = azurerm_linux_function_app.this.id
}

output "default_hostname" {
  value = azurerm_linux_function_app.this.default_hostname
}

output "image_reference" {
  value = "${data.azurerm_container_registry.this.login_server}/${var.image_name}:${var.image_tag}"
}

output "uami_client_id" {
  value = data.azurerm_user_assigned_identity.this.client_id
}

output "findings_dcr_id" {
  description = "Scope for the findings_ingest role (Monitoring Metrics Publisher) in identity/role-requirements.yaml."
  value       = azurerm_monitor_data_collection_rule.findings.id
}

output "findings_dcr_immutable_id" {
  value = azurerm_monitor_data_collection_rule.findings.immutable_id
}

output "logs_ingestion_endpoint" {
  value = azurerm_monitor_data_collection_endpoint.findings.logs_ingestion_endpoint
}

output "findings_tables" {
  value = keys(local.findings_tables)
}
