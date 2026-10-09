# ADR-0002: Dashboards, alerts, and Teams delivery live in this repo as a separate Terraform module

- Status: accepted
- Date: 2026-10-08

## Context

ADR-0001 moved notification and routing downstream of the findings tables, and the roadmap left open
whether those consumers live here or with Enterprise Observability. The pipeline now runs in production
(RHEL VM, Path C) and fills both tables, but nothing reads them yet. Teams retired Office 365 connectors
(incoming webhooks) in May 2026; a Teams channel now receives automated posts through a Teams Workflows
webhook that accepts Adaptive Cards only, which an action group cannot send directly.

## Decision

Workbooks, log search alert rules, action groups, and the Teams delivery Logic Apps live in this repo as
`terraform/module-o11y-insights`, separate from `module-azure-o11y`:

- **Separate module, one input that matters.** It reads only the Log Analytics workspace (and
  optionally App Insights), so it works with every deployment style, including the VM, and can be
  handed to Enterprise Observability later unchanged.
- **Same repo as the schema.** Every query is checked against `schema/findings-tables.json` by
  `tests/test_insights.py`, so a schema change and the queries it breaks travel in one PR.
- **Teams path:** alert rule → action group → Logic App (renders an Adaptive Card from the common
  alert schema) → Teams Workflows webhook. A weekly FinOps digest is a recurrence Logic App that queries
  the workspace itself. Workflow URLs carry a signature, so they are Key Vault secrets; the Logic Apps
  read them at run time with a user-assigned managed identity and mark the data secure in run history.
- **Routing is configuration.** Routes map `AssignmentGroup` values to Teams channels and email lists
  in tfvars; one catch-all route receives everything unrouted, including rows with no assignment group.
- **Terraform only.** No `deploy.sh` equivalent: these are downstream, change rarely, and the CI
  pipeline already runs Terraform.

## Consequences

- The Logic Apps need the existing `law` (Log Analytics Reader) and `secrets` (Key Vault Secrets User)
  rows of `identity/role-requirements.yaml`. They take a `uami_resource_id`; the function's UAMI works,
  or the IAM repo can create a dedicated one with only those two rows. No new role, no new row.
- The Teams Workflows (one per channel) are created by hand in Teams and their URLs stored in Key Vault
  by hand. Terraform references secret names only, so no URL reaches state or the repo.
- Ops alerts are stateful per (resource, metric): one card when a resource stays hot for
  `ops_sustained_runs` consecutive runs, one "resolved" card when it leaves the latest run. A mass event posts one
  card per pair.
- Pipeline health becomes alertable: missing runs and run errors from App Insights `traces` (when
  configured), and a stale FinOps table from the workspace alone.
- The "Downstream alerting on the findings tables" backlog item is closed. Datadog forwarding remains
  open.
