# Insights: dashboards, alerts, and Teams delivery

Read this before touching `terraform/module-o11y-insights/`, a query in it, or anything a team sees in
Teams. Decision record: [ADR-0002](../adr/0002-insights-module-in-this-repo.md). Operator runbook:
[docs/ops/insights.md](../ops/insights.md).

## What it is

A Terraform module, separate from `module-azure-o11y`, that reads the two findings tables and builds:

| Piece | Resource | Notes |
|---|---|---|
| Ops workbook | `azurerm_application_insights_workbook` | hot now with hot-since streaks, trend, top offenders, by team, routing gaps; pipeline health when `app_insights_id` is set |
| FinOps workbook | same | identified saving, by type / app / team, recommendations, daily trend, dropped-off candidates, tag hygiene |
| `o11y-ops-hot-<route>` | log search alert v2 | stateful per (resource, metric); fires after `ops_sustained_runs` consecutive runs, resolves when the pair leaves the latest run |
| `o11y-finops-new-<route>` | log search alert v2 | daily; candidates new since the previous run with saving ≥ `finops_new_saving_min` |
| `o11y-health-run-missing` / `-run-errors` | log search alert v2 on App Insights | only with `app_insights_id` |
| `o11y-health-finops-stale` | log search alert v2 on the workspace | liveness without App Insights |
| `ag-o11y-<channel>` | action groups | email receivers plus the channel's Teams Logic App |
| `logic-o11y-teams-<secret>` | Logic App (azapi) | common alert schema → Adaptive Card → Teams Workflows webhook |
| `logic-o11y-finops-digest-<route>` | Logic App (azapi) | weekly (default) digest card from two workspace queries |

It needs only the workspace, so it works with every deployment style; it never references the function
module. `terraform/examples/insights` is the root CI validates.

## Layout and rules

```
module-o11y-insights/
  queries/*.kql       pure KQL; Terraform prepends `let` inputs (route filter, windows, thresholds)
  workbooks/*.json    workbook JSON; __LAW_RESOURCE_ID__, __APP_INSIGHTS_ID__, __OPS_CADENCE_MINUTES__ substituted
  logicapps/*.json    workflow definitions; every value arrives as a workflow parameter
  samples/*.json      common-alert-schema payload for testing a channel
  tests/*.tftest.hcl  `terraform test` with mock providers (wiring, validations)
```

- **No Terraform template syntax in those files.** That keeps them checkable by
  `tests/test_insights.py`, which fails when a query names a column that is not in
  `schema/findings-tables.json` (or uses one table's column against the other), when a workflow's
  declared parameters differ from what `main.tf` passes, or when a query stops using an input Terraform
  prepends.
- **Schema changes travel with their queries.** Renaming or retyping a column is already forbidden
  ([findings](findings.md#changing-the-schema)); a new column used here must be in the schema first.
- **Routing is by `AssignmentGroup` only.** Each route claims groups; exactly one route is `catch_all`
  and also receives unclaimed and empty groups, so no row is unrouted. The filter is the same line in
  every route-scoped query:
  `AssignmentGroup in~ (route_groups) or (route_catch_all and AssignmentGroup !in~ (claimed_groups))`.
- **Secrets stay in Key Vault.** A Teams Workflows URL carries a signature, so it is a Key Vault secret
  read by the Logic App at run time through `uami_resource_id`. Both HTTP actions that touch it are
  `secureData`, so it never shows in run history. Terraform knows only the secret name.
- **No new identity, no new role.** The Logic Apps use the existing `law` (query the tables) and
  `secrets` (read the URL) rows of `identity/role-requirements.yaml`. Pass the function's UAMI or a
  dedicated one the IAM repo creates with only those two rows.
- Names live only in `locals.tf`, as in the function module.

## Alert mechanics

- Ops: `evaluation_frequency` = `ops_cadence_minutes`; the window is the smallest allowed size ≥
  `cadence × ops_sustained_runs + 10 min` (15 × 2 → `PT45M`). The query keeps pairs whose newest
  streak (consecutive runs; a gap over `1.5 × cadence` ends it, as in the workbook's *hot since*) has
  ≥ `sustained_runs` runs and that are present in the latest run (`> ago(cadence + 10m)`).
  Six dimensions (`ResourceId`, `ResourceName`, `MetricKey`, `Detail`, `AssignmentGroup`, `Owner`)
  make each pair its own alert; the measure is `ObservedValue` (Maximum, ≥ 0), so the card shows it.
  Changing an owner tag starts a new series (the old one resolves).
- FinOps new: frequency and window `P1D`, query range `P2D` (the v2 maximum). "New" = in the last day's
  run, not in the one before.
- Health: `run complete` is read in both log shapes, JSON in `message` (Functions host) or bare message
  plus `customDimensions` (OpenTelemetry on the VM). Error matching uses only this pipeline's own
  messages (`metrics chunk failed`, `resource type failed`, `findings write failed`,
  `missing … (need: …)`); keep them in step with `src/pipeline.py` and `src/bootstrap.py`.

## Teams card contract

`logicapps/teams-alert.json` renders any common-alert-schema payload generically: severity, state, time,
value, then every dimension as a fact. Buttons: filtered results, the resource (when a `ResourceId`
dimension exists), and `WorkbookUrl` / `RunbookUrl` from the rule's custom properties. A new rule
needs no workflow change; add dimensions to show more facts (at most six per rule).

The digest queries return `(Title, Value)` rows already formatted in KQL; the workflow only maps rows
to facts. Change the wording in the `.kql` files, not in the workflow.

## Checking a change

```bash
.venv/bin/python -m pytest tests/test_insights.py --no-cov
terraform -chdir=terraform/module-o11y-insights init -backend=false && terraform -chdir=terraform/module-o11y-insights test
terraform fmt -recursive terraform
```

A KQL syntax error only shows at `apply`, when Azure validates each alert query against the workspace
(`skip_query_validation` stays false). Paste a changed query into the workspace's Logs blade, with its
`let` inputs from `terraform console` or the rule, before opening the PR.
