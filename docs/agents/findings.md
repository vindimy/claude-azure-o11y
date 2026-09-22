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
  `Location`, `Sku`, `Tags`, `PortalUrl` (FinOps also has `OsType`, filled for VMs only)
- **Out-of-range metric:** `MetricNamespace`, `MetricName`, `MetricKey`, `Unit`, `Aggregation`,
  `ObservedValue`, `Threshold`, `ThresholdSource` (`config` or `tag`), `WindowStart`, `WindowEnd`,
  plus `LookbackMinutes` (Ops) or `LookbackDays`, `Granularity`,
  `Percentile`, `DataCoverage` (FinOps)
- **Recommendation (FinOps):** `RecommendedSku`, `Confidence`, `Reason`, `CurrentMonthlyCost`,
  `ProjectedMonthlyCost`, `EstimatedMonthlySaving`, `Currency`
- **Ownership:** `Owner`, `AssignmentGroup`, `AssignmentGroupEmail`, `CarId`, `MissingTags`

## Values per resource type

`ResourceType` is the ARM type in lower case; alert rules filter on it. `Sku` and `RecommendedSku` use
each type's own spelling. `Percentile` is 95, or 5 for inverted metrics (`hot_when: below`, e.g. VM
available memory), and `Aggregation` says how the series was obtained (`Average`, `Maximum`, `Total`,
`Average (derived)` for computed metrics such as SQL MI storage, `computed` for Resource Graph math).

| Type | `ResourceType` | `Sku` | `RecommendedSku` | `MetricKey` values |
|---|---|---|---|---|
| VM | `microsoft.compute/virtualmachines` | `Standard_D4s_v5` | SKU name | `cpu`, `memory`, `os_disk_iops`, `os_disk_bandwidth`, `vm_uncached_iops`, `vm_uncached_bandwidth` |
| SQL Database | `microsoft.sql/servers/databases` | `S3`, `GP_Gen5_8` | service objective / `GP_Gen5_4` | `dtu`, `cpu`, `app_cpu`, `storage`, `workers` |
| SQL Elastic Pool | `microsoft.sql/servers/elasticpools` | `StandardPool 100`, `GP_Gen5 8` | `StandardPool 50` | `dtu`, `cpu`, `storage` |
| SQL Managed Instance | `microsoft.sql/managedinstances` | `GP_Gen5 8 vCores` | `GP_Gen5 4 vCores` | `cpu`, `storage` |
| PostgreSQL Flexible | `microsoft.dbforpostgresql/flexibleservers` | `Standard_D4ds_v5` | SKU name | `cpu`, `memory`, `storage`, `disk_iops` |
| Cosmos DB | `microsoft.documentdb/databaseaccounts` | `provisioned` / `serverless` | `1200 RU/s`, `autoscale 4000 RU/s max` | `ru`, `throttled` |
| Event Hubs | `microsoft.eventhub/namespaces` | `Standard 4 TU`, `Premium 1 PU`, `Dedicated` | `Standard 2 TU` | `throttled`, `cpu`, `ingress` |
| VNET subnet | `microsoft.network/virtualnetworks/subnets` | the prefix list (`10.0.1.0/24`) | n/a (Ops-only) | `subnet_ip` |
| App Service Plan | `microsoft.web/serverfarms` | `P1v3 x3` | `P1v3 x2`, `P0v3 x3`, `delete` (no apps) | `cpu`, `memory`, `http_queue` |
| AKS cluster | `microsoft.containerservice/managedclusters` | `Standard_D4s_v5 x3 + Standard_D8s_v5 x2` (pools) | `system: Standard_D4s_v5 x1; userpool: min 1` (one change per pool, `; `-joined) | `node_cpu`, `node_memory`, `node_disk`, `unschedulable_pods` |
| Azure Cache for Redis | `microsoft.cache/redis` | `Standard C1`, `Premium P2` | `Standard C0` | `cpu`, `memory`, `server_load`, `errors` |
| Service Bus | `microsoft.servicebus/namespaces` | `Premium 2 MU`, `Standard`, `Basic` | `Premium 1 MU` | `throttled`, `server_errors`, `deadlettered`, `cpu`, `memory` |
| Application Gateway | `microsoft.network/applicationgateways` | `Standard_v2 autoscale 2-10`, `WAF_v2 x3`, `Standard_Medium x2` | `Standard_v2 autoscale 1-10`, `WAF_v2 x2` | `unhealthy_hosts`, `failed_requests`, `cpu`, `capacity` |
| Storage account | `microsoft.storage/storageaccounts` | `Standard_LRS Hot`, `Premium_LRS` | `Standard_LRS Cool` | `availability`, `throttled`, `transactions` |

VNET rows are one per **subnet**: `ResourceId` is the subnet id, `ResourceName` is `<vnet>/<subnet>`,
and `MetricNamespace` is `Microsoft.Network/virtualNetworks`.

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
excluded resource IDs, `RunId`, write failures, `type_failures` (a type whose inventory query failed;
the run continues with the others), and a `by_type` breakdown. Skip reasons: `ignored_rg`,
`excluded_by_tag`, `not_running` (VM, AKS, App Gateway), `not_online` (SQL DB), `not_ready` (pool, MI,
PostgreSQL, App Service Plan, Redis, Storage), `not_active` (Service Bus), `in_elastic_pool` (SQL DB,
FinOps), `no_capacity_model` (FinOps: Cosmos serverless, Event Hubs Dedicated, App Service Plan
Free/Shared, Service Bus Basic/Standard, App Gateway v1 or autoscale minimum 0), `not_tierable` (Storage,
FinOps: Premium, non-blob kinds, or already Cool/Cold/Archive), `chunk_failed`, `no_ops_data`,
`insufficient_finops_data`.

Counting differs by reason: `no_ops_data` is counted **per (resource, metric)** — one Ops run over three
VMs with six metrics can report 14 of them — while every other reason is counted **per resource**, once,
where the resource was dropped. A dashboard that sums `skips` across reasons is therefore not counting
resources; read `no_ops_data` on its own.

## Changing the schema

- Add columns only at the end of a table, and never rename or retype one. The table and the DCR stream
  must match, and existing queries depend on the names.
- Edit `schema/findings-tables.json` and the builder in `src/notify/findings.py` together.
  `tests/test_findings.py` fails if the row keys or types drift from the schema.
- Redeploy (either path) to update the table and DCR. Rows ingested before the change keep nulls in the
  new columns.
