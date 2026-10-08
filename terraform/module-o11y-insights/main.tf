# --- Dashboards ---------------------------------------------------------------------------------

resource "azurerm_application_insights_workbook" "this" {
  for_each = local.workbooks
  # Workbook names are GUIDs; derive a stable one so a re-apply updates rather than duplicates.
  name                = uuidv5("url", "https://o11y-insights/${lower(var.law_resource_id)}/${each.key}")
  resource_group_name = data.azurerm_resource_group.this.name
  location            = var.location
  display_name        = each.value.display_name
  source_id           = lower(var.law_resource_id)
  category            = "workbook"
  data_json           = local.workbook_json[each.key]
  tags                = local.tags
}

# --- Teams delivery: alert cards ----------------------------------------------------------------

# One per Teams channel (Key Vault secret). Action groups post the common alert schema here; the
# workflow renders an Adaptive Card and posts it to the channel's Teams Workflows webhook.
resource "azapi_resource" "teams_alert" {
  for_each  = local.teams_secrets
  type      = "Microsoft.Logic/workflows@2019-05-01"
  name      = local.teams_logic_name[each.key]
  parent_id = data.azurerm_resource_group.this.id
  location  = var.location
  tags      = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [var.uami_resource_id]
  }

  body = {
    properties = {
      state      = "Enabled"
      definition = jsondecode(file("${path.module}/logicapps/teams-alert.json"))
      parameters = {
        teamsWebhookSecretUri = { value = "${local.vault_uri}/secrets/${each.key}" }
        uamiResourceId        = { value = var.uami_resource_id }
      }
    }
  }
}

data "azapi_resource_action" "teams_alert_callback" {
  for_each                         = local.teams_secrets
  type                             = "Microsoft.Logic/workflows/triggers@2019-05-01"
  resource_id                      = "${azapi_resource.teams_alert[each.key].id}/triggers/manual"
  action                           = "listCallbackUrl"
  method                           = "POST"
  sensitive_response_export_values = ["value"]
}

# --- Teams delivery: FinOps digest --------------------------------------------------------------

resource "azapi_resource" "finops_digest" {
  for_each  = local.digests
  type      = "Microsoft.Logic/workflows@2019-05-01"
  name      = local.digest_logic_name[each.key]
  parent_id = data.azurerm_resource_group.this.id
  location  = var.location
  tags      = local.tags

  identity {
    type         = "UserAssigned"
    identity_ids = [var.uami_resource_id]
  }

  body = {
    properties = {
      state = "Enabled"
      definition = merge(local.digest_definition, {
        triggers = { Recurrence = { type = "Recurrence", recurrence = local.digest_recurrence } }
      })
      parameters = {
        teamsWebhookSecretUri = { value = "${local.vault_uri}/secrets/${each.value.teams_secret_name}" }
        uamiResourceId        = { value = var.uami_resource_id }
        workspaceId           = { value = data.azapi_resource.law.output.properties.customerId }
        summaryQuery = { value = join("\n", [
          local.route_lets[each.key],
          "let new_since = ${local.digest_period_days}d;",
          file("${path.module}/queries/finops-digest-summary.kql"),
        ]) }
        topQuery = { value = join("\n", [
          local.route_lets[each.key],
          "let top_n = ${var.finops_digest_top_n};",
          file("${path.module}/queries/finops-digest-top.kql"),
        ]) }
        title       = { value = "O11y FinOps digest: ${each.key}" }
        workbookUrl = { value = local.workbook_url.finops }
      }
    }
  }
}

# --- Alerts -------------------------------------------------------------------------------------

resource "azurerm_monitor_action_group" "this" {
  for_each            = local.action_groups
  name                = local.action_group_name[each.key]
  resource_group_name = data.azurerm_resource_group.this.name
  short_name          = substr("o11y-${each.key}", 0, 12)
  tags                = local.tags

  dynamic "email_receiver" {
    for_each = each.value.emails
    content {
      name                    = "email-${email_receiver.key}"
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }

  dynamic "logic_app_receiver" {
    for_each = each.value.teams_secret_name == "" ? [] : [each.value.teams_secret_name]
    content {
      name                    = "teams-${logic_app_receiver.value}"
      resource_id             = azapi_resource.teams_alert[logic_app_receiver.value].id
      callback_url            = data.azapi_resource_action.teams_alert_callback[logic_app_receiver.value].sensitive_output.value
      use_common_alert_schema = true
    }
  }
}

resource "azurerm_monitor_scheduled_query_rules_alert_v2" "this" {
  for_each                  = local.alert_rules
  name                      = local.alert_rule_name[each.key]
  resource_group_name       = data.azurerm_resource_group.this.name
  location                  = var.location
  display_name              = each.value.display_name
  description               = each.value.description
  severity                  = each.value.severity
  scopes                    = [each.value.scope]
  evaluation_frequency      = each.value.frequency
  window_duration           = each.value.window
  query_time_range_override = each.value.range_override == "" ? null : each.value.range_override
  enabled                   = var.alerts_enabled
  auto_mitigation_enabled   = each.value.auto_mitigate
  tags                      = local.tags

  criteria {
    query                   = each.value.query
    time_aggregation_method = each.value.aggregation
    metric_measure_column   = each.value.measure
    operator                = each.value.operator
    threshold               = each.value.threshold

    dynamic "dimension" {
      for_each = each.value.dimensions
      content {
        name     = dimension.value
        operator = "Include"
        values   = ["*"]
      }
    }

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  dynamic "action" {
    for_each = contains(keys(local.action_groups), each.value.action_group) ? [each.value.action_group] : []
    content {
      action_groups     = [azurerm_monitor_action_group.this[action.value].id]
      custom_properties = each.value.custom_properties
    }
  }
}
