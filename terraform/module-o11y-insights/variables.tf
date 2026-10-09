variable "resource_group_name" {
  description = "Existing resource group that receives the workbooks, alert rules, action groups, and Logic Apps."
  type        = string
}

variable "location" {
  description = "Azure region for the workbooks, alert rules, and Logic Apps (action groups are global)."
  type        = string
}

variable "law_resource_id" {
  description = "Log Analytics workspace that holds O11yOpsFindings_CL and O11yFinOpsFindings_CL (the function's law_resource_id)."
  type        = string
  validation {
    condition     = can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.OperationalInsights/workspaces/[^/]+$", var.law_resource_id))
    error_message = "law_resource_id must be a full Log Analytics workspace resource ID."
  }
}

variable "app_insights_id" {
  description = "Optional App Insights component that receives the pipeline's logs (app_insights_name). Enables the run-missing and run-errors alerts and the workbook's pipeline-health section."
  type        = string
  default     = ""
  validation {
    condition     = var.app_insights_id == "" || can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.Insights/components/[^/]+$", var.app_insights_id))
    error_message = "app_insights_id must be empty or a full Application Insights resource ID."
  }
}

variable "key_vault_id" {
  description = "Key Vault holding the Teams Workflows webhook URLs as secrets. Required when any teams_secret_name is set."
  type        = string
  default     = ""
  validation {
    condition = var.key_vault_id != "" || alltrue(concat(
      [for r in var.routes : try(r.ops.teams_secret_name, "") == "" && try(r.finops.teams_secret_name, "") == ""],
      [var.health.teams_secret_name == ""],
    ))
    error_message = "key_vault_id is required when a route or health sets teams_secret_name."
  }
  validation {
    condition     = var.key_vault_id == "" || can(regex("(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.KeyVault/vaults/[^/]+$", var.key_vault_id))
    error_message = "key_vault_id must be empty or a full Key Vault resource ID."
  }
}

variable "uami_resource_id" {
  description = "User-assigned identity for the Logic Apps (from the IAM repo): needs the law and secrets rows of identity/role-requirements.yaml. Required when any teams_secret_name is set."
  type        = string
  default     = ""
  validation {
    condition = var.uami_resource_id != "" || alltrue(concat(
      [for r in var.routes : try(r.ops.teams_secret_name, "") == "" && try(r.finops.teams_secret_name, "") == ""],
      [var.health.teams_secret_name == ""],
    ))
    error_message = "uami_resource_id is required when a route or health sets teams_secret_name."
  }
}

variable "routes" {
  description = <<-EOT
    Where Ops and FinOps findings go, keyed by a short route name. A route claims AssignmentGroup values;
    exactly one route is the catch_all and also receives every unclaimed or empty AssignmentGroup.
    ops / finops = null turns that channel off for the route. teams_secret_name is the Key Vault secret
    holding the channel's Teams Workflows webhook URL ("" = no Teams).
  EOT
  type = map(object({
    assignment_groups = optional(list(string), [])
    catch_all         = optional(bool, false)
    ops = optional(object({
      teams_secret_name = optional(string, "")
      emails            = optional(list(string), [])
      severity          = optional(number, 2)
    }))
    finops = optional(object({
      teams_secret_name = optional(string, "")
      emails            = optional(list(string), [])
      digest            = optional(bool, true)
      new_saving_alert  = optional(bool, true)
    }))
  }))
  validation {
    condition     = length([for r in var.routes : r if r.catch_all]) == 1
    error_message = "Exactly one route must set catch_all = true, so no finding is unrouted."
  }
  validation {
    condition     = alltrue([for k in keys(var.routes) : can(regex("^[a-z0-9][a-z0-9-]{0,19}$", k))])
    error_message = "Route names must be 1-20 lower-case letters, digits, or hyphens."
  }
  validation {
    condition = length(flatten([for r in var.routes : [for g in r.assignment_groups : lower(g)]])) == length(distinct(
      flatten([for r in var.routes : [for g in r.assignment_groups : lower(g)]])
    ))
    error_message = "An assignment group may be claimed by one route only."
  }
  validation {
    condition = alltrue(flatten([for r in var.routes : [
      for s in [try(r.ops.teams_secret_name, ""), try(r.finops.teams_secret_name, "")] : can(regex("^([0-9A-Za-z-]{1,60})?$", s))
    ]]))
    error_message = "teams_secret_name must be a Key Vault secret name of at most 60 characters."
  }
  validation {
    condition     = alltrue([for r in var.routes : try(r.ops.severity >= 0 && r.ops.severity <= 4, true)])
    error_message = "ops.severity must be 0-4."
  }
}

variable "health" {
  description = "Destination for the pipeline-health alerts (runs missing, run errors, FinOps table stale)."
  type = object({
    teams_secret_name = optional(string, "")
    emails            = optional(list(string), [])
  })
  default = {}
  validation {
    condition     = can(regex("^([0-9A-Za-z-]{1,60})?$", var.health.teams_secret_name))
    error_message = "health.teams_secret_name must be a Key Vault secret name of at most 60 characters."
  }
}

variable "ops_cadence_minutes" {
  description = "Minutes between Ops runs (ops_schedule_cron). Also the Ops alert evaluation frequency."
  type        = number
  default     = 15
  validation {
    condition     = contains([5, 10, 15, 30, 45, 60], var.ops_cadence_minutes)
    error_message = "ops_cadence_minutes must be 5, 10, 15, 30, 45, or 60 (an allowed alert evaluation frequency)."
  }
}

variable "ops_sustained_runs" {
  description = "Consecutive Ops runs a (resource, metric) must be hot before the Ops alert fires."
  type        = number
  default     = 2
  validation {
    condition     = var.ops_sustained_runs >= 1 && var.ops_sustained_runs <= 6 && floor(var.ops_sustained_runs) == var.ops_sustained_runs
    error_message = "ops_sustained_runs must be a whole number from 1 to 6."
  }
}

variable "finops_new_saving_min" {
  description = "Minimum EstimatedMonthlySaving (in the findings currency) for a new FinOps candidate to count in the daily new-savings alert."
  type        = number
  default     = 100
}

variable "finops_max_age_hours" {
  description = "Hours without a FinOps run (or FinOps row) before the health alerts fire; daily runs need a little over 24."
  type        = number
  default     = 26
  validation {
    condition     = var.finops_max_age_hours >= 2 && var.finops_max_age_hours <= 46
    error_message = "finops_max_age_hours must be 2-46 (the alert window is at most 2 days)."
  }
}

variable "finops_stale_alert_enabled" {
  description = "Alert when O11yFinOpsFindings_CL gets no row for finops_max_age_hours. Turn off for an estate with no cold resources."
  type        = bool
  default     = true
}

variable "finops_digest_schedule" {
  description = "When the FinOps digest posts to Teams (Logic Apps recurrence, UTC)."
  type = object({
    frequency = optional(string, "Week")
    interval  = optional(number, 1)
    week_days = optional(list(string), ["Monday"])
    hour      = optional(number, 14)
    minute    = optional(number, 0)
  })
  default = {}
  validation {
    condition     = contains(["Day", "Week"], var.finops_digest_schedule.frequency)
    error_message = "finops_digest_schedule.frequency must be Day or Week."
  }
  validation {
    condition = (
      var.finops_digest_schedule.interval >= 1 && floor(var.finops_digest_schedule.interval) == var.finops_digest_schedule.interval
      && var.finops_digest_schedule.interval * (var.finops_digest_schedule.frequency == "Week" ? 7 : 1) <= 30
    )
    error_message = "finops_digest_schedule.interval must be a whole number >= 1, and the period at most 30 days (the digest reads 30 days of history)."
  }
  validation {
    condition = (
      var.finops_digest_schedule.hour >= 0 && var.finops_digest_schedule.hour <= 23 && floor(var.finops_digest_schedule.hour) == var.finops_digest_schedule.hour
      && var.finops_digest_schedule.minute >= 0 && var.finops_digest_schedule.minute <= 59 && floor(var.finops_digest_schedule.minute) == var.finops_digest_schedule.minute
    )
    error_message = "finops_digest_schedule.hour must be 0-23 and minute 0-59, whole numbers (UTC)."
  }
  validation {
    condition = var.finops_digest_schedule.frequency != "Week" || (
      length(var.finops_digest_schedule.week_days) > 0
      && alltrue([for d in var.finops_digest_schedule.week_days : contains(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"], d)])
    )
    error_message = "A weekly finops_digest_schedule needs week_days from Monday..Sunday (capitalized)."
  }
}

variable "finops_digest_top_n" {
  description = "Recommendations listed in the FinOps digest card."
  type        = number
  default     = 10
  validation {
    condition     = var.finops_digest_top_n >= 1 && var.finops_digest_top_n <= 25 && floor(var.finops_digest_top_n) == var.finops_digest_top_n
    error_message = "finops_digest_top_n must be a whole number 1-25 (Teams cards are limited to about 28 KB)."
  }
}

variable "alerts_enabled" {
  description = "Create the alert rules enabled. Set false for a first deploy, check the workbooks, then enable."
  type        = bool
  default     = true
}

variable "runbook_url" {
  description = "Optional link shown on every Teams alert card (e.g. docs/ops/insights.md in the repo browser)."
  type        = string
  default     = ""
}
