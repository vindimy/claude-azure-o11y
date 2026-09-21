# Findings tables (Log Analytics)

Read this before changing `src/notify/`, `schema/findings-tables.json`, the findings DCR/DCE, or what the
teams see. Decision record: [ADR-0001](../adr/0001-findings-to-log-analytics.md).

## What gets written

The function sends no notifications and writes no reports. It writes one row per finding to one of two
custom tables in the workspace named by `law_resource_id`:

| Table | Written when | Cadence |
|-------|--------------|---------|
| `O11yOpsFindings_CL` | a metric is at or above its hot threshold over the Ops lookback | every Ops run (`ops_schedule_cron`, default every 15 min) while hot |
| `O11yFinOpsFindings_CL` | a metric's percentile over the FinOps lookback is below its cold threshold | every FinOps run (`finops_schedule_cron`, default daily 06:00 UTC); take the latest `RunId` per resource |

The two runs are separate timer functions in the same app, `o11y_ops` and `o11y_finops`. Each fetches
only its own metric window and writes only its own table.

The full column list, with types and descriptions, lives in **`schema/findings-tables.json`**. That file is
the only definition. The Terraform module, `deploy.sh`, and the tests all read it, and the descriptions
are published to the workspace. Column groups:

- **Run:** `TimeGenerated` (run time), `RunId`, `ManagementGroupId`
- **Resource:** `ResourceId`, `ResourceName`, `ResourceType`, `SubscriptionId`, `ResourceGroup`,
  `Location`, `Sku`, `Tags`, `PortalUrl` (FinOps also has `OsType`)
- **Out-of-range metric:** `MetricNamespace`, `MetricName`, `MetricKey`, `Unit`, `Aggregation`,
  `ObservedValue`, `Threshold`, `ThresholdSource` (`config` or `tag`), `WindowStart`, `WindowEnd`,
  plus `LookbackMinutes` (Ops) or `LookbackDays`, `Granularity`,
  `Percentile`, `DataCoverage` (FinOps)
- **Recommendation (FinOps):** `RecommendedSku`, `Confidence`, `Reason`, `CurrentMonthlyCost`,
  `ProjectedMonthlyCost`, `EstimatedMonthlySaving`, `Currency`
- **Ownership:** `Owner`, `AssignmentGroup`, `AssignmentGroupEmail`, `CarId`, `MissingTags`

## Ownership tags

Tag names are configurable (`tags:` in the thresholds config). These are the defaults:

| Tag | Column | Notes |
|-----|--------|-------|
| `owner` | `Owner` | Email. A malformed address is listed in `MissingTags` |
| `assignment_group` | `AssignmentGroup`, `AssignmentGroupEmail` | The email comes from `config/assignment-groups.yaml`; an unmapped group is listed in `MissingTags` |
| `car_id` | `CarId` | Application ID; the FinOps grouping key |

`config/assignment-groups.yaml` (`<assignment_group>: {email}`) is maintained by hand and validated at
startup. Unknown keys and malformed emails fail loudly. Never guess an owner. `MissingTags` makes tag
hygiene queryable.

## Routing is downstream

Paging, email, and Teams are built on the tables, outside this repo: Azure Monitor log search alert rules
and action groups, workbooks, or Datadog. Filter on `Owner`, `AssignmentGroup`, `CarId`, or
`ManagementGroupId` to route. Useful queries:

```kusto
// Ops: hot now, and for how long (one row per resource per Ops run)
O11yOpsFindings_CL
| where TimeGenerated > ago(4h)
| summarize FirstSeen = min(TimeGenerated), arg_max(TimeGenerated, ObservedValue, Threshold, Owner, AssignmentGroupEmail, PortalUrl) by ResourceId, MetricName
| where TimeGenerated > ago(20m)   // present in the latest Ops run

// FinOps: latest recommendation per resource, grouped by application
O11yFinOpsFindings_CL
| where TimeGenerated > ago(2d)
| summarize arg_max(TimeGenerated, *) by ResourceId
| summarize Saving = sum(EstimatedMonthlySaving), Resources = count() by CarId, AssignmentGroup, Currency
```

## How rows get there

`pipeline.py` builds rows with the pure builders in `src/notify/findings.py` and hands them to a
`FindingsSink`:

- **Live:** `src/storage/law.py` calls the Logs Ingestion API through the findings DCE
  (`LOGS_INGESTION_ENDPOINT`) and DCR (`FINDINGS_DCR_IMMUTABLE_ID`), stream `Custom-<table>`. The UAMI
  needs `Monitoring Metrics Publisher` on the DCR (`findings_ingest` in `identity/role-requirements.yaml`).
- **Dry run:** `src/notify/sinks.py` appends JSON lines to `./out/findings/<table>.jsonl`.

Each run writes its table in one call. A failed write ends the run with `FindingsWriteFailed`; the next
scheduled run writes fresh rows (nothing is replayed). A 403 raises `PermissionMissing("findings_ingest")`.

Runs are summarized in the structured `run complete` log (App Insights): counts, skips, ignored-RG count,
excluded resource IDs, `RunId`, and write failures.

## Changing the schema

- Add columns only at the end of a table, and never rename or retype one. The table and the DCR stream
  must match, and existing queries depend on the names.
- Edit `schema/findings-tables.json` and the builder in `src/notify/findings.py` together.
  `tests/test_findings.py` fails if the row keys or types drift from the schema.
- Redeploy (either path) to update the table and DCR. Rows ingested before the change keep nulls in the
  new columns.
