data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}

data "azurerm_user_assigned_identity" "this" {
  name                = var.uami_name
  resource_group_name = var.resource_group_name
}

data "azurerm_storage_account" "this" {
  name                = var.storage_account_name
  resource_group_name = var.resource_group_name
}

data "azurerm_key_vault" "this" {
  name                = var.key_vault_name
  resource_group_name = var.resource_group_name
}

data "azurerm_container_registry" "this" {
  name                = var.acr_name
  resource_group_name = var.resource_group_name
}

data "azurerm_application_insights" "this" {
  count               = var.app_insights_name == "" ? 0 : 1
  name                = var.app_insights_name
  resource_group_name = var.resource_group_name
}
