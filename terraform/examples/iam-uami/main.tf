# REFERENCE ONLY: a template for the external IAM repo, which owns the UAMI and its role assignments.
# Nothing in this repo applies it, and module-azure-o11y never creates identities (see
# docs/agents/identity.md). The role assignments come straight from identity/role-requirements.yaml,
# so this example stays in step with the request the IAM repo receives.
#
# Order on a fresh environment:
#   1. Apply with findings_dcr_id = "" (UAMI + every role except findings_ingest).
#   2. Deploy the function (module-azure-o11y or scripts/deploy.sh); it creates the findings DCR.
#   3. Re-apply with findings_dcr_id = <module output findings_dcr_id> to grant findings_ingest.

resource "azurerm_user_assigned_identity" "o11y" {
  name                = var.uami_name
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = { workload = "o11y-alerting" }
}

locals {
  requirements = yamldecode(file("${path.module}/../../../identity/role-requirements.yaml")).requirements

  placeholders = {
    "{management_group_id}" = "/providers/Microsoft.Management/managementGroups/${var.management_group_id}"
    "{law_resource_id}"     = var.law_resource_id
    "{storage_account_id}"  = var.storage_account_id
    "{key_vault_id}"        = var.key_vault_id
    "{acr_id}"              = var.acr_id
    "{findings_dcr_id}"     = var.findings_dcr_id
  }

  # A row whose scope needs an empty placeholder is skipped (and listed in the skipped output).
  resolvable = {
    for r in local.requirements : r.id => r
    if alltrue([for k, v in local.placeholders : v != "" || !strcontains(r.scope, k)])
  }

  assignments = {
    for id, r in local.resolvable : id => {
      role    = r.role
      purpose = r.purpose
      scope = replace(replace(replace(replace(replace(replace(r.scope,
        "{management_group_id}", local.placeholders["{management_group_id}"]),
        "{law_resource_id}", var.law_resource_id),
        "{storage_account_id}", var.storage_account_id),
        "{key_vault_id}", var.key_vault_id),
        "{acr_id}", var.acr_id),
      "{findings_dcr_id}", var.findings_dcr_id)
    }
  }
}

resource "azurerm_role_assignment" "o11y" {
  for_each             = local.assignments
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.o11y.principal_id
  principal_type       = "ServicePrincipal"
  description          = "o11y-alerting ${each.key}: ${each.value.purpose}"
}
