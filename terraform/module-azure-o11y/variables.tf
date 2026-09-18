variable "resource_group_name" {
  description = "Existing resource group that receives the plan, function app, and findings DCE/DCR."
  type        = string
}

variable "location" {
  description = "Azure region for the plan and function app."
  type        = string
}

variable "uami_name" {
  description = "Existing user-assigned managed identity (created by the IAM repo), in the resource group."
  type        = string
}

variable "storage_account_name" {
  description = "Existing storage account for Functions host storage (identity-based; the app keeps no other state)."
  type        = string
}

variable "key_vault_name" {
  description = "Existing Key Vault for app-setting secret references (none used today; SMTP/Datadog later)."
  type        = string
}

variable "management_group_id" {
  description = "Management group to iterate; also selects config/thresholds/<id>.yaml."
  type        = string
}

variable "subscription_ids" {
  description = "Optional comma-separated subscription IDs; when set, scope is these subscriptions instead of the MG."
  type        = string
  default     = ""
}

variable "law_resource_id" {
  description = "Existing Log Analytics workspace resource ID. Receives the findings tables; also the source of VM guest metrics later."
  type        = string
  validation {
    condition     = can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.OperationalInsights/workspaces/[^/]+$", var.law_resource_id))
    error_message = "law_resource_id must be a full Log Analytics workspace resource ID."
  }
}

variable "acr_name" {
  description = "Existing Azure Container Registry name (UAMI needs AcrPull)."
  type        = string
}

variable "image_name" {
  description = "Repository name of the function image in the ACR."
  type        = string
  default     = "o11y-alerting"
}

variable "image_tag" {
  description = "Immutable image tag built by CI (commit SHA). Changing it is the deploy."
  type        = string
}

variable "function_app_name" {
  description = "Function app name; empty uses the default in locals.tf."
  type        = string
  default     = ""
}

variable "app_service_plan_name" {
  description = "Plan name; empty uses the default in locals.tf."
  type        = string
  default     = ""
}

variable "plan_sku" {
  description = "Elastic Premium (EP1/EP2/EP3) or Dedicated SKU. Consumption is not valid for containers."
  type        = string
  default     = "EP1"
  validation {
    condition     = can(regex("^(EP[123]|P[0-9]+v[23]|S[123]|B[123])$", var.plan_sku))
    error_message = "plan_sku must be an Elastic Premium or Dedicated SKU."
  }
}

variable "ops_schedule_cron" {
  description = "NCRONTAB schedule (UTC) for the Ops run; every run writes a row per hot resource."
  type        = string
  default     = "0 */15 * * * *"
}

variable "finops_schedule_cron" {
  description = "NCRONTAB schedule (UTC) for the FinOps run; daily by default."
  type        = string
  default     = "0 0 6 * * *"
}

variable "dry_run" {
  description = "When true, findings are written as JSON lines inside the container instead of to Log Analytics."
  type        = bool
  default     = false
}

variable "app_insights_name" {
  description = "Optional existing Application Insights component in the resource group."
  type        = string
  default     = ""
}
