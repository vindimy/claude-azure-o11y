output "workbook_urls" {
  description = "Portal links to the Ops and FinOps workbooks."
  value       = local.workbook_url
}

output "workbook_ids" {
  value = { for k, w in azurerm_application_insights_workbook.this : k => w.id }
}

output "alert_rule_ids" {
  value = { for k, r in azurerm_monitor_scheduled_query_rules_alert_v2.this : k => r.id }
}

output "action_group_ids" {
  value = { for k, a in azurerm_monitor_action_group.this : k => a.id }
}

output "teams_logic_app_ids" {
  description = "Alert-card Logic App per Teams secret. Its trigger URL is a secret: read it with az rest listCallbackUrl when testing (docs/ops/insights.md)."
  value       = { for k, w in azapi_resource.teams_alert : k => w.id }
}

output "finops_digest_logic_app_ids" {
  value = { for k, w in azapi_resource.finops_digest : k => w.id }
}

output "teams_secret_names" {
  description = "Key Vault secrets that must hold a Teams Workflows webhook URL before the first alert or digest."
  value       = sort(distinct(concat(tolist(local.teams_secrets), [for d in values(local.digests) : d.teams_secret_name])))
}
