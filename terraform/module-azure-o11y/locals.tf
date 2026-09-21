# Naming lives here only, so the bank's naming standard can be applied in one place.
locals {
  function_app_name     = var.function_app_name != "" ? var.function_app_name : "func-o11y-alerting"
  app_service_plan_name = var.app_service_plan_name != "" ? var.app_service_plan_name : "asp-o11y-alerting"
  dce_name              = "dce-o11y-findings"
  dcr_name              = "dcr-o11y-findings"
  registry_url          = "https://${data.azurerm_container_registry.this.login_server}"

  # Shared with src/notify/findings.py and scripts/deploy.sh. DCR streams spell the type "datetime";
  # the Tables API wants "dateTime".
  findings_tables = jsondecode(file("${path.module}/../../schema/findings-tables.json")).tables

  app_settings = merge(
    {
      FUNCTIONS_WORKER_RUNTIME            = "python"
      WEBSITES_ENABLE_APP_SERVICE_STORAGE = "false"
      AzureWebJobsStorage__clientId       = data.azapi_resource.uami.output.properties.clientId
      AZURE_CLIENT_ID                     = data.azapi_resource.uami.output.properties.clientId
      MG_ID                               = var.management_group_id
      SUBSCRIPTION_IDS                    = var.subscription_ids
      LAW_RESOURCE_ID                     = var.law_resource_id
      OPS_SCHEDULE_CRON                   = var.ops_schedule_cron
      FINOPS_SCHEDULE_CRON                = var.finops_schedule_cron
      DRY_RUN                             = tostring(var.dry_run)
      CONFIG_DIR                          = "config"
      IDENTITY_FILE                       = "identity/role-requirements.yaml"
      LOGS_INGESTION_ENDPOINT             = azurerm_monitor_data_collection_endpoint.findings.logs_ingestion_endpoint
      FINDINGS_DCR_IMMUTABLE_ID           = azurerm_monitor_data_collection_rule.findings.immutable_id
    },
    var.app_insights_name == "" ? {} : {
      APPLICATIONINSIGHTS_CONNECTION_STRING = data.azurerm_application_insights.this[0].connection_string
    }
  )

  tags = {
    workload = "o11y-alerting"
  }
}
