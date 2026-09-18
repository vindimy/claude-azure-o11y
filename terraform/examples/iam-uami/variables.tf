variable "resource_group_name" {
  description = "Resource group for the UAMI. module-azure-o11y looks the UAMI up by name in its own resource_group_name, so use the same RG."
  type        = string
}

variable "location" {
  description = "Region for the UAMI."
  type        = string
}

variable "uami_name" {
  description = "UAMI name; passed to module-azure-o11y / deploy.sh as uami_name."
  type        = string
  default     = "id-o11y-alerting"
}

variable "management_group_id" {
  description = "Management group the function iterates (inventory + metrics roles)."
  type        = string
}

variable "law_resource_id" {
  description = "Log Analytics workspace resource ID (law row: guest metrics reads)."
  type        = string
}

variable "storage_account_id" {
  description = "Storage account resource ID (host storage and suppression container rows)."
  type        = string
}

variable "key_vault_id" {
  description = "Key Vault resource ID (secrets row)."
  type        = string
}

variable "acr_id" {
  description = "Container registry resource ID (acr_pull row)."
  type        = string
}

variable "findings_dcr_id" {
  description = "Findings DCR resource ID (module-azure-o11y output findings_dcr_id). Empty skips findings_ingest until the DCR exists."
  type        = string
  default     = ""
}
