# Naming lives here only, so the bank's naming standard can be applied in one place.
locals {
  workbooks = {
    ops    = { display_name = "O11y Ops findings", file = "ops.json" }
    finops = { display_name = "O11y FinOps recommendations", file = "finops.json" }
  }
  action_group_name = { for k in keys(local.action_groups) : k => "ag-o11y-${k}" }
  alert_rule_name   = { for k in keys(local.alert_rules) : k => "o11y-${k}" }
  teams_logic_name  = { for s in local.teams_secrets : s => "logic-o11y-teams-${s}" }
  digest_logic_name = { for k in keys(local.digests) : k => "logic-o11y-finops-digest-${k}" }

  tags = {
    workload = "o11y-alerting"
  }

  # --- Routing -------------------------------------------------------------------------------
  # Every route-scoped query starts with these lets; see queries/*.kql for the filter that uses them.
  claimed_groups = sort(distinct(flatten([for r in var.routes : r.assignment_groups])))
  route_lets = { for k, r in var.routes : k => join("\n", [
    "let route_groups = dynamic(${jsonencode(r.assignment_groups)});",
    "let route_catch_all = ${r.catch_all};",
    "let claimed_groups = dynamic(${jsonencode(local.claimed_groups)});",
  ]) }

  # --- Durations -----------------------------------------------------------------------------
  # Window sizes a log search alert accepts, in minutes.
  iso_duration = {
    "5"   = "PT5M", "10" = "PT10M", "15" = "PT15M", "30" = "PT30M", "45" = "PT45M", "60" = "PT1H",
    "120" = "PT2H", "180" = "PT3H", "240" = "PT4H", "300" = "PT5H", "360" = "PT6H", "1440" = "P1D",
  }
  # The window must hold `ops_sustained_runs` runs plus ingestion latency.
  ops_window_needed  = var.ops_cadence_minutes * var.ops_sustained_runs + 10
  ops_window_minutes = min([for m in [5, 10, 15, 30, 45, 60, 120, 180, 240, 300, 360, 1440] : m if m >= local.ops_window_needed]...)
  ops_frequency      = local.iso_duration[tostring(var.ops_cadence_minutes)]

  # --- Teams ---------------------------------------------------------------------------------
  vault_uri = var.key_vault_id == "" ? "" : trimsuffix(data.azapi_resource.key_vault[0].output.properties.vaultUri, "/")

  # Notification channels behind action groups: name => { emails, teams_secret_name }.
  action_groups = merge(
    { for k, r in var.routes : "ops-${k}" => { emails = r.ops.emails, teams_secret_name = r.ops.teams_secret_name } if r.ops != null },
    { for k, r in var.routes : "finops-${k}" => { emails = r.finops.emails, teams_secret_name = r.finops.teams_secret_name } if try(r.finops.new_saving_alert, false) },
    length(var.health.emails) > 0 || var.health.teams_secret_name != "" ? { health = { emails = var.health.emails, teams_secret_name = var.health.teams_secret_name } } : {},
  )

  # One alert-card Logic App per distinct Teams secret behind an action group.
  teams_secrets = toset(compact([for ag in values(local.action_groups) : ag.teams_secret_name]))

  # One digest Logic App per route with a FinOps Teams channel.
  digests = { for k, r in var.routes : k => r.finops if try(r.finops.digest && r.finops.teams_secret_name != "", false) }

  # The trigger lives in Terraform so the schedule is a variable; the rest of the workflow is static JSON.
  digest_definition  = jsondecode(file("${path.module}/logicapps/finops-digest.json"))
  digest_period_days = var.finops_digest_schedule.interval * (var.finops_digest_schedule.frequency == "Week" ? 7 : 1)
  digest_recurrence = {
    frequency = var.finops_digest_schedule.frequency
    interval  = var.finops_digest_schedule.interval
    timeZone  = "UTC"
    schedule = merge(
      { hours = [var.finops_digest_schedule.hour], minutes = [var.finops_digest_schedule.minute] },
      { for k, v in { weekDays = var.finops_digest_schedule.week_days } : k => v if var.finops_digest_schedule.frequency == "Week" },
    )
  }

  # --- Alert rules ---------------------------------------------------------------------------
  workbook_url = { for k, w in azurerm_application_insights_workbook.this : k => "https://portal.azure.com/#resource${w.id}/workbook" }
  links = {
    ops    = merge({ WorkbookUrl = local.workbook_url.ops }, var.runbook_url == "" ? {} : { RunbookUrl = var.runbook_url })
    finops = merge({ WorkbookUrl = local.workbook_url.finops }, var.runbook_url == "" ? {} : { RunbookUrl = var.runbook_url })
  }

  alert_rules = merge(
    { for k, r in var.routes : "ops-hot-${k}" => {
      display_name   = "O11y Ops: hot resource (${k})"
      description    = "A resource stayed at or above its hot threshold for ${var.ops_sustained_runs} consecutive Ops run(s) (route ${k}). Resolves when it leaves the latest run."
      severity       = r.ops.severity
      scope          = var.law_resource_id
      frequency      = local.ops_frequency
      window         = local.iso_duration[tostring(local.ops_window_minutes)]
      range_override = ""
      query = join("\n", [
        local.route_lets[k],
        "let cadence = ${var.ops_cadence_minutes}m;",
        "let sustained_runs = ${var.ops_sustained_runs};",
        "let fresh_after = ${var.ops_cadence_minutes + 10}m;",
        file("${path.module}/queries/ops-hot.kql"),
      ])
      measure           = "ObservedValue"
      aggregation       = "Maximum"
      operator          = "GreaterThanOrEqual"
      threshold         = 0
      dimensions        = ["ResourceId", "ResourceName", "MetricKey", "Detail", "AssignmentGroup", "Owner"]
      auto_mitigate     = true
      action_group      = "ops-${k}"
      custom_properties = local.links.ops
    } if r.ops != null },
    { for k, r in var.routes : "finops-new-${k}" => {
      display_name   = "O11y FinOps: new downsizing candidates (${k})"
      description    = "New FinOps recommendations since the previous daily run with an estimated saving of at least ${var.finops_new_saving_min} a month each (route ${k})."
      severity       = 3
      scope          = var.law_resource_id
      frequency      = "P1D"
      window         = "P1D"
      range_override = "P2D"
      query = join("\n", [
        local.route_lets[k],
        "let min_saving = ${var.finops_new_saving_min};",
        file("${path.module}/queries/finops-new-savings.kql"),
      ])
      measure           = "NewMonthlySaving"
      aggregation       = "Total"
      operator          = "GreaterThan"
      threshold         = 0
      dimensions        = ["Currency", "NewResources"]
      auto_mitigate     = false
      action_group      = "finops-${k}"
      custom_properties = local.links.finops
    } if try(r.finops.new_saving_alert, false) },
    var.app_insights_id == "" ? {} : {
      health-run-missing = {
        display_name   = "O11y health: pipeline runs missing"
        description    = "No 'run complete' log for a run mode within its expected interval (ops ${3 * var.ops_cadence_minutes} min, finops ${var.finops_max_age_hours} h). Check the timers or the Function App and the journal."
        severity       = 1
        scope          = var.app_insights_id
        frequency      = "PT15M"
        window         = "PT15M"
        range_override = "P2D"
        query = join("\n", [
          "let ops_max_age = ${3 * var.ops_cadence_minutes}m;",
          "let finops_max_age = ${var.finops_max_age_hours}h;",
          file("${path.module}/queries/health-run-missing.kql"),
        ])
        measure           = "MinutesSinceLastRun"
        aggregation       = "Maximum"
        operator          = "GreaterThan"
        threshold         = 0
        dimensions        = ["Mode"]
        auto_mitigate     = true
        action_group      = "health"
        custom_properties = local.links.ops
      }
      health-run-errors = {
        display_name      = "O11y health: pipeline run errors"
        description       = "A run reported write, resource-type, or metrics-chunk failures, or a missing permission. See the run complete log and docs/ops for the fix."
        severity          = 2
        scope             = var.app_insights_id
        frequency         = "PT15M"
        window            = "PT30M"
        range_override    = ""
        query             = file("${path.module}/queries/health-run-errors.kql")
        measure           = "Occurrences"
        aggregation       = "Total"
        operator          = "GreaterThan"
        threshold         = 0
        dimensions        = ["Problem"]
        auto_mitigate     = true
        action_group      = "health"
        custom_properties = local.links.ops
      }
    },
    var.finops_stale_alert_enabled ? {
      health-finops-stale = {
        display_name   = "O11y health: FinOps table stale"
        description    = "O11yFinOpsFindings_CL got no row for ${var.finops_max_age_hours} h: the daily FinOps run failed, did not run, or found nothing cold."
        severity       = 3
        scope          = var.law_resource_id
        frequency      = "PT1H"
        window         = "PT1H"
        range_override = "P2D"
        query = join("\n", [
          "let max_age = ${var.finops_max_age_hours}h;",
          file("${path.module}/queries/health-finops-stale.kql"),
        ])
        measure           = "HoursSinceLastRow"
        aggregation       = "Maximum"
        operator          = "GreaterThan"
        threshold         = 0
        dimensions        = []
        auto_mitigate     = true
        action_group      = "health"
        custom_properties = local.links.finops
      }
    } : {},
  )

  # --- Workbooks -----------------------------------------------------------------------------
  workbook_doc = { for k, w in local.workbooks : k => jsondecode(replace(replace(replace(
    file("${path.module}/workbooks/${w.file}"),
    "__LAW_RESOURCE_ID__", var.law_resource_id),
    "__APP_INSIGHTS_ID__", var.app_insights_id),
    "__OPS_CADENCE_MINUTES__", tostring(var.ops_cadence_minutes)
  )) }
  # Without App Insights, drop the pipeline-health group rather than show failing queries.
  workbook_json = { for k, d in local.workbook_doc : k => jsonencode(merge(d, {
    items = [for i in d.items : i if var.app_insights_id != "" || i.name != "group - pipeline health"]
  })) }
}
