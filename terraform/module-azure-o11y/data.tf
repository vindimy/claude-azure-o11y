data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}

# The UAMI comes from the IAM repo and may live in another RG or subscription, so read it by ARM ID.
data "azapi_resource" "uami" {
  type                   = "Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31"
  resource_id            = var.uami_resource_id
  response_export_values = ["properties.clientId"]
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

# The workspace may live in another subscription, so read it by ARM ID rather than name + RG.
data "azapi_resource" "law" {
  type        = "Microsoft.OperationalInsights/workspaces@2022-10-01"
  resource_id = var.law_resource_id
}
