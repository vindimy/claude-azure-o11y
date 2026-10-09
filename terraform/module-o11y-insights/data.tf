data "azurerm_resource_group" "this" {
  name = var.resource_group_name
}

# The workspace may live in another subscription, so read it by ARM ID. customerId is the GUID the
# Log Analytics query API (FinOps digest) addresses.
data "azapi_resource" "law" {
  type                   = "Microsoft.OperationalInsights/workspaces@2022-10-01"
  resource_id            = var.law_resource_id
  response_export_values = ["properties.customerId"]
}

data "azapi_resource" "key_vault" {
  count                  = var.key_vault_id == "" ? 0 : 1
  type                   = "Microsoft.KeyVault/vaults@2023-07-01"
  resource_id            = var.key_vault_id
  response_export_values = ["properties.vaultUri"]
}
