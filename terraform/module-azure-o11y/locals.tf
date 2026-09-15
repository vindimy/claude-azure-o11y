# Naming lives here only, so the bank's naming standard can be applied in one place.
locals {
  function_app_name     = var.function_app_name != "" ? var.function_app_name : "func-o11y-alerting"
  app_service_plan_name = var.app_service_plan_name != "" ? var.app_service_plan_name : "asp-o11y-alerting"
  containers            = ["reports", "suppression"]
  registry_url          = "https://${data.azurerm_container_registry.this.login_server}"

  app_settings = merge(
    {
      FUNCTIONS_WORKER_RUNTIME            = "python"
      WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
      AzureWebJobsStorage__clientId       = data.azurerm_user_assigned_identity.this.client_id
      AZURE_CLIENT_ID                     = data.azurerm_user_assigned_identity.this.client_id
      MG_ID                               = var.management_group_id
      SUBSCRIPTION_IDS                    = var.subscription_ids
      LAW_RESOURCE_ID                     = var.law_resource_id
      STORAGE_ACCOUNT_NAME                = var.storage_account_name
      SCHEDULE_CRON                       = var.schedule_cron
      DRY_RUN                             = tostring(var.dry_run)
      CONFIG_DIR                          = "config"
      IDENTITY_FILE                       = "identity/role-requirements.yaml"
      OPS_TEAMS_WEBHOOK_URL               = "@Microsoft.KeyVault(VaultName=${var.key_vault_name};SecretName=${var.ops_webhook_secret_name})"
    },
    var.app_insights_name == "" ? {} : {
      APPLICATIONINSIGHTS_CONNECTION_STRING = data.azurerm_application_insights.this[0].connection_string
    }
  )

  tags = {
    workload = "o11y-alerting"
  }
}
