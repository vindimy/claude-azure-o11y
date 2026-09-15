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
