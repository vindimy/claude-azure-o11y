resource "azurerm_service_plan" "this" {
  name                = local.app_service_plan_name
  resource_group_name = data.azurerm_resource_group.this.name
  location            = var.location
  os_type             = "Linux"
  sku_name            = var.plan_sku
  tags                = local.tags
}

resource "azurerm_storage_container" "this" {
  for_each              = toset(local.containers)
  name                  = each.value
  storage_account_id    = data.azurerm_storage_account.this.id
  container_access_type = "private"
}

resource "azurerm_linux_function_app" "this" {
  name                = local.function_app_name
  resource_group_name = data.azurerm_resource_group.this.name
  location            = var.location
  service_plan_id     = azurerm_service_plan.this.id
  tags                = local.tags

  storage_account_name          = data.azurerm_storage_account.this.name
  storage_uses_managed_identity = true
  functions_extension_version   = "~4"
  https_only                    = true

  identity {
    type         = "UserAssigned"
    identity_ids = [data.azurerm_user_assigned_identity.this.id]
  }
  key_vault_reference_identity_id = data.azurerm_user_assigned_identity.this.id

  site_config {
    container_registry_use_managed_identity       = true
    container_registry_managed_identity_client_id = data.azurerm_user_assigned_identity.this.client_id
    application_stack {
      docker {
        registry_url = local.registry_url
        image_name   = var.image_name
        image_tag    = var.image_tag
      }
    }
  }

  app_settings = local.app_settings

  lifecycle {
    ignore_changes = [
      app_settings["WEBSITE_RUN_FROM_PACKAGE"],
    ]
  }
}
