# Offline checks of the module's wiring: mock providers, no Azure credentials.
# Run: terraform -chdir=terraform/module-o11y-insights init -backend=false && terraform -chdir=terraform/module-o11y-insights test

mock_provider "azurerm" {
  mock_data "azurerm_resource_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test"
    }
  }
  mock_resource "azurerm_monitor_action_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test/providers/Microsoft.Insights/actionGroups/mock"
    }
  }
  mock_resource "azurerm_application_insights_workbook" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test/providers/Microsoft.Insights/workbooks/mock"
    }
  }
}

mock_provider "azapi" {
  mock_resource "azapi_resource" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test/providers/Microsoft.Logic/workflows/mock"
    }
  }
}

override_data {
  target = data.azapi_resource.law
  values = { output = { properties = { customerId = "11111111-1111-1111-1111-111111111111" } } }
}

override_data {
  target = data.azapi_resource.key_vault
  values = { output = { properties = { vaultUri = "https://kv-o11y-alerting.vault.azure.net/" } } }
}

override_data {
  target = data.azapi_resource_action.teams_alert_callback
  values = { sensitive_output = { value = "https://example.invalid/callback" } }
}

variables {
  resource_group_name = "rg-o11y-test"
  location            = "centralus"
  law_resource_id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-monitoring/providers/Microsoft.OperationalInsights/workspaces/law-central"
  app_insights_id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test/providers/Microsoft.Insights/components/appi-o11y"
  key_vault_id        = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test/providers/Microsoft.KeyVault/vaults/kv-o11y-alerting"
  uami_resource_id    = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-o11y-test/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-o11y-alerting"
  routes = {
    platform = {
      assignment_groups = ["cloud-platform"]
      catch_all         = true
      ops               = { teams_secret_name = "teams-cloud-ops", emails = ["oncall@example.com"] }
      finops            = { teams_secret_name = "teams-cloud-finops" }
    }
    data = {
      assignment_groups = ["data-platform", "dba"]
      ops               = { teams_secret_name = "teams-data-ops", severity = 1 }
      finops            = { teams_secret_name = "teams-cloud-finops", new_saving_alert = false }
    }
  }
  health = { teams_secret_name = "teams-cloud-ops", emails = ["eng@example.com"] }
}

run "full_configuration" {
  command = apply

  assert {
    condition     = length(azurerm_application_insights_workbook.this) == 2
    error_message = "Expected the Ops and FinOps workbooks."
  }
  assert {
    condition = toset(keys(azurerm_monitor_scheduled_query_rules_alert_v2.this)) == toset([
      "ops-hot-platform", "ops-hot-data", "finops-new-platform",
      "health-run-missing", "health-run-errors", "health-finops-stale",
    ])
    error_message = "Unexpected alert rules."
  }
  assert {
    condition     = toset(keys(azurerm_monitor_action_group.this)) == toset(["ops-platform", "ops-data", "finops-platform", "health"])
    error_message = "Unexpected action groups."
  }
  assert {
    condition     = toset(keys(azapi_resource.teams_alert)) == toset(["teams-cloud-ops", "teams-data-ops", "teams-cloud-finops"])
    error_message = "Expected one alert-card Logic App per distinct Teams secret behind an action group."
  }
  assert {
    condition     = toset(keys(azapi_resource.finops_digest)) == toset(["platform", "data"])
    error_message = "Expected one digest per route with a FinOps Teams channel."
  }
  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-platform"].window_duration == "PT45M"
    error_message = "15-minute cadence x 2 runs + 10 min needs the 45-minute window."
  }
  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-data"].severity == 1
    error_message = "Route severity was not applied."
  }
  assert {
    condition     = startswith(azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-data"].criteria[0].query, "let route_groups = dynamic([\"data-platform\",\"dba\"]);\nlet route_catch_all = false;")
    error_message = "Route lets must open the query."
  }
  assert {
    condition     = strcontains(azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-platform"].criteria[0].query, "let claimed_groups = dynamic([\"cloud-platform\",\"data-platform\",\"dba\"]);")
    error_message = "The catch-all route must exclude every claimed group."
  }
  assert {
    condition     = length(azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-platform"].criteria[0].dimension) == 6
    error_message = "Ops alerts split by six dimensions."
  }
  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.this["health-run-missing"].query_time_range_override == "P2D"
    error_message = "Run-missing must look back two days to see the daily FinOps run."
  }
  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.this["health-run-missing"].scopes[0] == var.app_insights_id
    error_message = "Run health reads App Insights."
  }
  assert {
    condition     = azapi_resource.teams_alert["teams-data-ops"].body.properties.parameters.teamsWebhookSecretUri.value == "https://kv-o11y-alerting.vault.azure.net/secrets/teams-data-ops"
    error_message = "Secret URI must be versionless and built from the vault URI."
  }
  assert {
    condition     = jsonencode(azapi_resource.finops_digest["platform"].body.properties.definition.triggers.Recurrence.recurrence.schedule.weekDays) == jsonencode(["Monday"])
    error_message = "Weekly digest must carry its week days."
  }
  assert {
    condition     = strcontains(azapi_resource.finops_digest["platform"].body.properties.parameters.summaryQuery.value, "let new_since = 7d;")
    error_message = "A weekly digest counts candidates new in the last 7 days."
  }
  assert {
    condition     = length([for i in jsondecode(azurerm_application_insights_workbook.this["ops"].data_json).items : i if i.name == "group - pipeline health"]) == 1
    error_message = "With App Insights the Ops workbook keeps its pipeline-health group."
  }
  assert {
    condition     = !strcontains(azurerm_application_insights_workbook.this["ops"].data_json, "__")
    error_message = "Every workbook placeholder must be substituted."
  }
  assert {
    condition     = length(azurerm_monitor_action_group.this["ops-platform"].logic_app_receiver) == 1 && length(azurerm_monitor_action_group.this["ops-platform"].email_receiver) == 1
    error_message = "The ops action group needs its Teams Logic App and email receivers."
  }
}

run "without_app_insights_or_teams" {
  command = apply

  variables {
    app_insights_id  = ""
    key_vault_id     = ""
    uami_resource_id = ""
    routes = {
      all = {
        catch_all = true
        ops       = { emails = ["oncall@example.com"] }
        finops    = { emails = ["finops@example.com"] }
      }
    }
    health                     = {}
    finops_digest_schedule     = { frequency = "Day" }
    finops_stale_alert_enabled = true
  }

  assert {
    condition     = toset(keys(azurerm_monitor_scheduled_query_rules_alert_v2.this)) == toset(["ops-hot-all", "finops-new-all", "health-finops-stale"])
    error_message = "Without App Insights only the workspace health rule remains."
  }
  assert {
    condition     = length(azapi_resource.teams_alert) == 0 && length(azapi_resource.finops_digest) == 0
    error_message = "No Teams secret means no Logic App."
  }
  assert {
    condition     = length(azurerm_monitor_scheduled_query_rules_alert_v2.this["health-finops-stale"].action) == 0
    error_message = "Health has no receivers, so its rule has no action group."
  }
  assert {
    condition     = length([for i in jsondecode(azurerm_application_insights_workbook.this["ops"].data_json).items : i if i.name == "group - pipeline health"]) == 0
    error_message = "Without App Insights the pipeline-health group is dropped."
  }
  assert {
    condition     = strcontains(azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-all"].criteria[0].query, "let route_groups = dynamic([]);")
    error_message = "A catch-all route with no groups gets an empty list."
  }
}

run "cadence_and_sustain_pick_window" {
  command = plan

  variables {
    ops_cadence_minutes = 30
    ops_sustained_runs  = 3
  }

  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-platform"].window_duration == "PT2H"
    error_message = "30 min x 3 + 10 = 100 min needs the 2-hour window."
  }
  assert {
    condition     = azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-platform"].evaluation_frequency == "PT30M"
    error_message = "Ops alerts evaluate at the run cadence."
  }
}

run "rejects_two_catch_all_routes" {
  command = plan

  variables {
    routes = {
      a = { catch_all = true, ops = {} }
      b = { catch_all = true, ops = {} }
    }
  }

  expect_failures = [var.routes]
}

run "rejects_group_claimed_twice" {
  command = plan

  variables {
    routes = {
      a = { catch_all = true, assignment_groups = ["dba"], ops = {} }
      b = { assignment_groups = ["DBA"], ops = {} }
    }
  }

  expect_failures = [var.routes]
}

run "requires_vault_and_identity_for_teams" {
  command = plan

  variables {
    key_vault_id     = ""
    uami_resource_id = ""
  }

  expect_failures = [var.key_vault_id, var.uami_resource_id]
}

run "ops_query_counts_consecutive_runs" {
  command = plan

  assert {
    condition     = strcontains(azurerm_monitor_scheduled_query_rules_alert_v2.this["ops-hot-platform"].criteria[0].query, "let cadence = 15m;\nlet sustained_runs = 2;")
    error_message = "The Ops query needs the cadence to tell consecutive runs from gaps."
  }
}

run "rejects_bad_digest_schedule" {
  command = plan

  variables {
    finops_digest_schedule = { frequency = "Week", interval = 5, week_days = ["Funday"], hour = 24 }
  }

  expect_failures = [var.finops_digest_schedule]
}

run "rejects_fractional_top_n" {
  command = plan

  variables {
    finops_digest_top_n = 2.5
  }

  expect_failures = [var.finops_digest_top_n]
}
