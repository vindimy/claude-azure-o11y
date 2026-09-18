resource "azurerm_service_plan" "this" {
  name                = local.app_service_plan_name
  resource_group_name = data.azurerm_resource_group.this.name
  location            = var.location
  os_type             = "Linux"
  sku_name            = var.plan_sku
  tags                = local.tags
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

# --- Findings: Log Analytics custom tables fed by the Logs Ingestion API -----------------------

resource "azapi_resource" "findings_table" {
  for_each  = local.findings_tables
  type      = "Microsoft.OperationalInsights/workspaces/tables@2022-10-01"
  name      = each.key
  parent_id = var.law_resource_id
  body = {
    properties = {
      plan = "Analytics"
      schema = {
        name        = each.key
        description = each.value.description
        columns = [for c in each.value.columns : {
          name        = c.name
          type        = c.type == "datetime" ? "dateTime" : c.type
          description = c.description
        }]
      }
    }
  }
}

# The DCE and DCR must be in the workspace's region.
resource "azurerm_monitor_data_collection_endpoint" "findings" {
  name                = local.dce_name
  resource_group_name = data.azurerm_resource_group.this.name
  location            = data.azapi_resource.law.location
  tags                = local.tags
}

resource "azurerm_monitor_data_collection_rule" "findings" {
  name                        = local.dcr_name
  resource_group_name         = data.azurerm_resource_group.this.name
  location                    = data.azapi_resource.law.location
  data_collection_endpoint_id = azurerm_monitor_data_collection_endpoint.findings.id
  tags                        = local.tags

  destinations {
    log_analytics {
      name                  = "law"
      workspace_resource_id = var.law_resource_id
    }
  }

  dynamic "stream_declaration" {
    for_each = local.findings_tables
    content {
      stream_name = "Custom-${stream_declaration.key}"
      dynamic "column" {
        for_each = stream_declaration.value.columns
        content {
          name = column.value.name
          type = column.value.type
        }
      }
    }
  }

  dynamic "data_flow" {
    for_each = local.findings_tables
    content {
      streams       = ["Custom-${data_flow.key}"]
      destinations  = ["law"]
      transform_kql = "source"
      output_stream = "Custom-${data_flow.key}"
    }
  }

  # The output streams must exist before the DCR references them.
  depends_on = [azapi_resource.findings_table]
}
