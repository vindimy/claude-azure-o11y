# Design: Ops and FinOps dashboards, alerts, and Teams delivery

Date: 2026-10-08
Status: approved for implementation (autonomous session; assumptions listed in §7)
Decision record: [ADR-0002](../../adr/0002-insights-module-in-this-repo.md)

## 1. Goal

The pipeline fills `O11yOpsFindings_CL` and `O11yFinOpsFindings_CL`. Build what reads them:

| Need | Built as |
|---|---|
| Ops dashboard | Workbook "O11y Ops findings": hot now, hot-since streaks, trend, top offenders, routing gaps, pipeline health |
| FinOps dashboard | Workbook "O11y FinOps recommendations": identified saving, by app / team / type, recommendation list, trend, dropped-off candidates, tag hygiene |
| Ops alerts | One log alert per route; fires per (resource, metric) after `ops_sustained_runs` consecutive Ops runs, resolves when the pair leaves the latest run |
| FinOps alerts | One daily log alert per route: new candidates since the previous FinOps run with saving ≥ `finops_new_saving_min` |
| Pipeline health alerts | Runs missing per mode, run errors (App Insights, optional), FinOps table stale (workspace only) |
| Teams | Adaptive Card per alert (fired and resolved) through a Teams Workflows webhook; weekly FinOps digest card per route |

Success: `terraform validate` passes for the module through `terraform/examples/insights`, `make test`
and `make lint` pass, every query only names columns from `schema/findings-tables.json`, and no
secret, URL, or identity resource is added to the repo or to Terraform state as a literal.

## 2. Shape

```
terraform/module-o11y-insights/
  versions.tf variables.tf locals.tf data.tf main.tf outputs.tf
  queries/*.kql          # pure KQL; inputs are `let` statements Terraform prepends
  workbooks/*.json       # workbook JSON; __LAW_RESOURCE_ID__ / __APP_INSIGHTS_ID__ substituted
  logicapps/*.json       # workflow definitions; values arrive as workflow parameters
  samples/*.json         # common-alert-schema payloads for testing a Teams channel
terraform/examples/insights/   # root that calls the module (CI validates it)
```

Keeping KQL, workbooks, and workflow definitions as static files with no Terraform template syntax
lets `tests/test_insights.py` parse and check them without Terraform.

## 3. Routing

```hcl
routes = {
  platform = {
    assignment_groups = ["cloud-platform", "cloud-network"]
    catch_all         = true            # also gets every AssignmentGroup no route claims, and ""
    ops    = { teams_secret_name = "teams-platform-ops", emails = ["oncall@bank.com"], severity = 2 }
    finops = { teams_secret_name = "teams-platform-finops", emails = [] }
  }
}
```

A route's KQL filter is `AssignmentGroup in~ (route_groups) or (route_catch_all and AssignmentGroup
!in~ (claimed_groups))`. Exactly one catch-all route is required, so no row is unrouted (validated).
`ops` or `finops` set to `null` turns that channel off for the route. `health` is a separate
destination (`{ teams_secret_name, emails }`) for the pipeline-health rules.

## 4. Alerts

All rules are `azurerm_monitor_scheduled_query_rules_alert_v2` on the workspace (health rules that need
logs scope App Insights). Query inputs are prepended as `let`s, so the files stay pure KQL.

- **`ops-hot-<route>`**: every `ops_cadence_minutes` (default 15). The window is the smallest allowed
  duration ≥ `cadence × sustained + 10 min`. The query keeps pairs whose newest streak of consecutive runs (a gap over
  `1.5 × cadence` ends it) has ≥ `ops_sustained_runs` runs and that are in the latest run
  (`> ago(cadence + 10m)`). Split by `ResourceId`,
  `ResourceName`, `MetricKey`, `Detail` (`"<MetricName> (threshold <n> <unit>)"`), `AssignmentGroup`,
  and `Owner`, so each pair is its own stateful alert. The measure is `ObservedValue` (Maximum, ≥ 0,
  always true), so the card shows the value. Auto-mitigation is on.
- **`finops-new-<route>`**: daily, window P2D (the v2 maximum). "New" means in the last day's run and
  not in the run before it. Saving ≥ `finops_new_saving_min` (default 100). Not split; it fires once per
  route per day with the count and total. Stateless (no auto-mitigation).
- **`health-run-missing`** (App Insights): split by mode; ops is late after `3 × cadence`, finops after
  `finops_max_age_hours` (26). It reads `run complete` from `traces` in both shapes: JSON in `message`
  (Functions host) or OpenTelemetry `customDimensions` (VM).
- **`health-run-errors`** (App Insights): `run complete` with `write_failures`, `type_failures`, or
  `chunk_failures` > 0, or the pipeline's own error lines (`metrics chunk failed`, `resource type
  failed`, `findings write failed`, `missing … (need: …)`). Matching our messages keeps a shared App
  Insights component from paging us for other apps.
- **`health-finops-stale`** (workspace): no FinOps row in `finops_max_age_hours`. The only liveness
  signal when App Insights is not configured. Off by `finops_stale_alert_enabled = false` for an estate
  that legitimately has no cold resources.

## 5. Teams delivery

- **`logic-o11y-teams-<secret>`** (one per distinct Teams secret): an HTTP trigger called by action
  groups with the common alert schema. Reads the Workflows URL from Key Vault (MI, secure outputs) and
  builds an Adaptive Card 1.4: header coloured by state/severity, facts (severity, state, time, value, every
  dimension), and buttons (filtered results, the resource in the portal when a `ResourceId` dimension
  exists, `WorkbookUrl` / `RunbookUrl` custom properties). Then it posts `{type: message, attachments:
  [adaptive card]}`.
- **`logic-o11y-finops-digest-<route>`**: recurrence (default Monday 14:00 UTC). It runs two queries through
  the Log Analytics query API (MI, audience `https://api.loganalytics.io`), each returning `(Title,
  Value)` rows preformatted in KQL, so the workflow only maps rows to facts. It posts a card with the
  summary, the top `finops_digest_top_n` recommendations, and a workbook button. It posts even when the
  route has no candidates (a weekly sign of life).
- Action groups: `ag-o11y-<route>-ops`, `ag-o11y-<route>-finops`, `ag-o11y-health`, each with the email
  receivers (common schema) and the channel's Logic App receiver.

## 6. Dashboards

Workbooks are `azurerm_application_insights_workbook` resources, `source_id` = the workspace (lower case),
with a stable `uuidv5` name. Parameters: time range, workspace (defaults to the deployed one),
subscription, resource type, assignment group (FinOps also CarId and confidence). The Ops pipeline-health
section shows only when an App Insights component is selected.

## 7. Assumptions (autonomous session)

1. Downstream consumers live here (ADR-0002), Terraform only.
2. Teams channels use Teams Workflows webhooks ("When a Teams webhook request is received"), created by
   hand. URLs are Key Vault secrets in an existing vault (`key_vault_id`).
3. The Logic Apps reuse a UAMI from the IAM repo (`uami_resource_id`). The existing `law` and `secrets`
   rows cover them; their purposes are updated, no row is added.
4. Routing is by `AssignmentGroup`; `CarId` and `Owner` are shown and filterable but do not route.
5. Ops cadence is an input (`ops_cadence_minutes`, default 15) rather than parsed from NCRONTAB, because
   the module does not depend on the function module.
6. The released VM package in `releases/` is v3; the schema used is the current
   `schema/findings-tables.json`.

## 8. Testing

- `tests/test_insights.py`: (a) every `.kql` file and every workbook query names only columns of the
  tables it reads, plus names it defines itself; (b) workbooks are valid `Notebook/1.0` JSON with the
  substitution tokens; (c) workflow definitions declare exactly the parameters Terraform passes; (d) the
  example root forwards every module variable.
- CI `lint:terraform` also validates `terraform/examples/insights`.
- Live check after apply: post `samples/common-alert-ops.json` to a channel's callback URL; run the
  digest Logic App by hand.
