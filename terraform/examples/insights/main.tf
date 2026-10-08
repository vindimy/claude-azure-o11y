module "insights" {
  source = "../../module-o11y-insights"

  resource_group_name        = var.resource_group_name
  location                   = var.location
  law_resource_id            = var.law_resource_id
  app_insights_id            = var.app_insights_id
  key_vault_id               = var.key_vault_id
  uami_resource_id           = var.uami_resource_id
  routes                     = var.routes
  health                     = var.health
  ops_cadence_minutes        = var.ops_cadence_minutes
  ops_sustained_runs         = var.ops_sustained_runs
  finops_new_saving_min      = var.finops_new_saving_min
  finops_max_age_hours       = var.finops_max_age_hours
  finops_stale_alert_enabled = var.finops_stale_alert_enabled
  finops_digest_schedule     = var.finops_digest_schedule
  finops_digest_top_n        = var.finops_digest_top_n
  alerts_enabled             = var.alerts_enabled
  runbook_url                = var.runbook_url
}

output "workbook_urls" {
  value = module.insights.workbook_urls
}

output "teams_secret_names" {
  value = module.insights.teams_secret_names
}

output "alert_rule_ids" {
  value = module.insights.alert_rule_ids
}
