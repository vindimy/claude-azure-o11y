# Architecture

Read this before changing the pipeline, adding a client, or questioning a design choice.

## Why this exists

Diagnostic Settings are not shipped at scale, and there is no central metric store across
subscriptions. This function fills that gap by querying each resource's built-in platform metrics
directly. Customers: the Ops team, the FinOps team, and the app teams that own resources (routed by tag).

**Why not Azure Advisor?** It was rejected on purpose. It is too generic, it has no Event Hub or VNET
coverage, it has no routing by ownership, and it gives no control over thresholds.

## Pipeline

```
Timer trigger: o11y_ops / o11y_finops (OPS_SCHEDULE_CRON / FINOPS_SCHEDULE_CRON app settings)
  └─ Inventory: Azure Resource Graph, one query per resource type, MG-scoped
       └─ Drop ignored resource groups (config/ignore.yaml), before any metrics call
            └─ Group by (subscription, region, resource type)
                 └─ Metrics: metrics:getBatch (regional endpoint), ≤50 resource IDs per call
                      └─ Evaluate against this MG's thresholds
                           ├─ Ops hot → O11yOpsFindings_CL
                           └─ FinOps cold → recommendation + savings → O11yFinOpsFindings_CL
                                (both via the Logs Ingestion API: DCE → findings DCR → LAW)
```

## Metrics: batch, never per resource

Use the Metrics Batch API
(`https://{region}.metrics.monitor.azure.com/subscriptions/{subId}/metrics:getBatch`) through
`azure-monitor-querymetrics` `MetricsClient.query_resources()`. `azure-monitor-query` 2.x has no
metrics client (see `docs/gotchas.md`).

- One call covers one resource type, one region, and one subscription, with up to **50 resource IDs**.
- Resource Graph supplies the inventory (type, region, SKU, tags), so batches are built without
  per-subscription ARM listing.
- VM guest metrics (memory, disk) come from **one** KQL query against the central LAW, filtered to the
  resource IDs in scope.
- VNET utilization comes from Resource Graph arithmetic only, with no Monitor calls.

## Scale

Expect 30–50 subscriptions per MG. One function instance handles a full MG in a single run, fanning out
per subscription with `asyncio` plus a semaphore. The last-resort fallback is one function per
subscription. Scope is switchable through config (`MG_ID` vs `SUBSCRIPTION_IDS`), so that fallback needs
no code change.

## Resource types

| # | Type | Ops signal (hot) | FinOps signal (cold) | Metric source |
|---|------|------------------|----------------------|---------------|
| 1 | Virtual Machines (**MVP: CPU only**) | CPU, memory, disk space | Low CPU/memory → smaller SKU in same family | Platform: `Percentage CPU`. Guest: AMA→central LAW (`InsightsMetrics` / `Perf`) for memory + disk |
| 2 | Azure SQL Database / Elastic Pool | CPU %, DTU/vCore %, storage % | Low DTU/vCore → lower tier | Platform |
| 3 | Azure SQL Managed Instance | CPU %, storage % | Low vCore usage → fewer vCores | Platform |
| 4 | PostgreSQL Flexible Server | CPU, memory, storage % | Low CPU/memory → smaller compute tier | Platform |
| 5 | Cosmos DB | Normalized RU consumption, storage | Low normalized RU → lower provisioned RU or autoscale | Platform (`NormalizedRUConsumption`, `ProvisionedThroughput`) |
| 6 | Event Hubs Namespace | Throttled requests, CPU (Premium/Dedicated) | Low incoming bytes/msgs vs TU/PU/CU → lower tier / fewer units | Platform |
| 7 | Virtual Networks | Subnet IP utilization ≥ threshold | none (capacity, not cost) | Resource Graph math: address space minus allocated IPs minus 5 Azure-reserved per subnet |

Guest-OS metrics apply to VMs only.

## Out of scope

- **History beyond the findings tables.** Runs keep no other state. Storage holds only Functions host
  state. Never add a database or another table; findings history is the
  LAW tables (ADR-0001).
- **Sending notifications.** Teams, email, and paging are built on the tables downstream.
- **Auto-remediation.** The system recommends; humans resize.
- **Azure Advisor and Cost Management integration.**

## Code boundaries

- Every Azure call goes through a thin client in `inventory/`, `metrics/`, or `storage/` (Logs
  Ingestion), behind the Protocols in `src/ports.py` and `src/notify/base.py`. `evaluate/`, `recommend/`,
  and `notify/` (findings row builders) are SDK-free and take plain data (`src/models.py`), so tests can
  swap the clients.
- The one exception is `recommend/pricing.py`, the `RetailPriceClient` for the unauthenticated Retail
  Prices API. It takes an injected `httpx.AsyncClient`, needs no identity, and never fails the run.
- Code makes no `azure-cli` calls.
- Logging is structured JSON to App Insights. Log every skipped resource with a reason.
