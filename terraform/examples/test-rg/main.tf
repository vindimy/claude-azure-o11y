module "o11y" {
  source = "../../module-azure-o11y"

  resource_group_name     = var.resource_group_name
  location                = var.location
  uami_name               = var.uami_name
  storage_account_name    = var.storage_account_name
  key_vault_name          = var.key_vault_name
  management_group_id     = var.management_group_id
  subscription_ids        = var.subscription_ids
  law_resource_id         = var.law_resource_id
  acr_name                = var.acr_name
  image_name              = var.image_name
  image_tag               = var.image_tag
  function_app_name       = var.function_app_name
  app_service_plan_name   = var.app_service_plan_name
  plan_sku                = var.plan_sku
  schedule_cron           = var.schedule_cron
  dry_run                 = var.dry_run
  ops_webhook_secret_name = var.ops_webhook_secret_name
  app_insights_name       = var.app_insights_name
}

output "function_app_name" {
  value = module.o11y.function_app_name
}

output "image_reference" {
  value = module.o11y.image_reference
}
