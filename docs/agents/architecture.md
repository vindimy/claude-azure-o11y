# Architecture

Read this before changing the pipeline, adding a client, or questioning a design choice.

## Why this exists

Diagnostic Settings are not shipped at scale, and there is no central metric store across
subscriptions. This function fills that gap by querying each resource's built-in platform metrics
directly. Customers: the Ops team, the FinOps team, and the app teams that own resources (routed by tag).

**Why not Azure Advisor?** It was rejected on purpose. It is too generic, it has no Event Hub or VNET
coverage, it has no routing by ownership, and it gives no control over thresholds.

## Pipeline

One run is one mode. The two schedulers start the same `bootstrap.main(mode)`; the mode picks the metric
window, the evaluator, and the table.

```mermaid
flowchart TB
  subgraph sched["Schedulers, one per run mode"]
    ops["ops: OPS_SCHEDULE_CRON<br/>default every 15 min"]
    finops["finops: FINOPS_SCHEDULE_CRON<br/>default daily 06:00 UTC"]
  end
  ops & finops --> boot["bootstrap.main(mode)<br/>Settings from env, config/ for this MG,<br/>DefaultAzureCredential(AZURE_CLIENT_ID)"]
  boot --> run["pipeline.run(mode)"]
  run --> inv["inventory/: Azure Resource Graph<br/>one query per resource type, MG or subscription scope"]
  inv --> filt["inventory/filters<br/>drop ignored RGs (config/ignore.yaml)<br/>and o11y-exclude=true resources"]
  filt --> grp["group by (subscription, region)<br/>chunks of 50 resource IDs"]
  grp --> met["metrics/: metrics:getBatch on the regional endpoint<br/>asyncio fan-out bounded by MAX_CONCURRENCY"]
  met --> eval["evaluate/: this MG's thresholds<br/>default.yaml + mg-id.yaml, tag overrides"]
  eval -->|ops: at or above hot| opsrow["notify/findings.ops_row"]
  eval -->|finops: percentile below cold| rec["recommend/: smaller SKU in the same family<br/>+ Retail Prices monthly cost and saving"]
  rec --> finrow["notify/findings.finops_row"]
  opsrow & finrow --> sink["FindingsSink, one write per run"]
  sink -->|DRY_RUN=false| law["storage/law.py: Logs Ingestion API<br/>DCE, findings DCR, stream Custom-table"]
  sink -->|DRY_RUN=true| files["notify/sinks.py<br/>OUTPUT_DIR/findings/table.jsonl"]
  law --> t1[("O11yOpsFindings_CL")]
  law --> t2[("O11yFinOpsFindings_CL")]
  t1 & t2 --> down["Downstream, outside this repo:<br/>log alert rules, action groups, workbooks, Datadog"]
```

The order of calls in one run, and where it can stop:

```mermaid
sequenceDiagram
  participant S as Scheduler
  participant P as pipeline.run
  participant RG as Resource Graph
  participant M as Metrics Batch API
  participant RP as Retail Prices
  participant LI as Logs Ingestion (DCE + DCR)
  S->>P: main(mode)
  P->>RG: list_vms(scope) with scope = MG_ID, or SUBSCRIPTION_IDS when set
  RG-->>P: VMs with type, region, SKU, tags, power state (403: PermissionMissing inventory)
  P->>P: filter ignored RGs and excluded tags, group by (sub, region), chunk by 50
  par one task per chunk, at most MAX_CONCURRENCY in flight
    P->>M: query(region, sub, ids, Percentage CPU, window for this mode)
    M-->>P: points per resource id (a failed chunk skips its VMs with chunk_failed)
  end
  P->>P: evaluate_hot or evaluate_cold per VM, log every skip with a reason
  opt finops only
    P->>RP: monthly_price(region, current SKU) and monthly_price(region, target SKU)
    RP-->>P: prices, or None (pricing never fails the run)
  end
  P->>LI: upload rows to Custom-table, once per run
  LI-->>P: ok, or 403 as PermissionMissing findings_ingest, or FindingsWriteFailed
  P-->>S: run complete log line + RunSummary
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

## Deployment styles

The same `src/`, `config/`, and `identity/` run in three places. Only the scheduler, the way the code
arrives, the identity, and where logs go differ. Operator runbooks: [docs/ops/](../ops/README.md).

```mermaid
flowchart TB
  subgraph vm["RHEL 9 VM (Path C)"]
    direction TB
    v0["code: git archive release<br/>/opt/o11y-alerting/releases/commit-sha + venv<br/>vm-install.sh + Ansible switch the current symlink"] --> v1["scheduler: systemd timers<br/>o11y-alerting-ops.timer, o11y-alerting-finops.timer<br/>NCRONTAB converted to OnCalendar, UTC"]
    v1 --> v2["entry: o11y-alerting@mode.service<br/>src/run_local.py with RUN_MODES=mode<br/>→ bootstrap.main(mode)"]
    v2 --> v3["identity: UAMI attached to the VM<br/>token from IMDS"]
    v2 --> v4["logs: journald<br/>+ OpenTelemetry export to App Insights"]
    v2 --> v5["findings: Logs Ingestion<br/>DRY_RUN: /var/lib/o11y-alerting/out"]
  end

  subgraph fa["Azure Function App (production)"]
    direction TB
    f0["code: container image<br/>acr/o11y-alerting:commit-sha<br/>deploy.sh or Terraform points the app at a tag"] --> f1["scheduler: Functions timer triggers<br/>o11y_ops, o11y_finops"]
    f1 --> f2["entry: src/function_app.py<br/>one function per mode<br/>→ bootstrap.main(mode)"]
    f2 --> f3["identity: UAMI attached to the app<br/>also pulls the image, owns host storage,<br/>resolves Key Vault references"]
    f2 --> f4["logs: Functions host → App Insights"]
    f2 --> f5["findings: Logs Ingestion<br/>DRY_RUN: files inside the container"]
  end

  subgraph mac["Local Mac (development)"]
    direction TB
    m0["code: git checkout + .venv"] --> m1["scheduler: you<br/>scripts/run-once.sh"]
    m1 --> m2["entry: src/run_local.py<br/>RUN_MODES=ops,finops in one process<br/>→ bootstrap.main(mode)"]
    m2 --> m3["identity: az login user"]
    m2 --> m4["logs: JSON lines on stderr"]
    m2 --> m5["findings: ./out/findings/*.jsonl<br/>--live: Logs Ingestion"]
  end
```

| | Local Mac | Function App | RHEL 9 VM |
|---|---|---|---|
| Scheduler | you | Functions timers (`%OPS_SCHEDULE_CRON%`, `%FINOPS_SCHEDULE_CRON%`) | systemd timers rendered from the same NCRONTAB |
| Entry point | `src/run_local.py`, both modes in sequence | `src/function_app.py`, one timer function per mode | `src/run_local.py`, one service instance per mode |
| Code arrives as | git checkout | image tag in the ACR (CI or `build-image.sh`) | `git archive` release, own venv per release |
| Deploy tool | none | `scripts/deploy.sh` (dev) or `terraform/module-azure-o11y` (CI) | `scripts/vm-install.sh` + `ansible/` |
| Identity | `az login` user | UAMI attached to the app, `AZURE_CLIENT_ID` | UAMI attached to the VM, `AZURE_CLIENT_ID` |
| Settings | env / `.env` / `run-once.sh` flags | app settings | `/etc/o11y-alerting/o11y-alerting.env` |
| Findings, live | Logs Ingestion (`--live`) | Logs Ingestion | Logs Ingestion |
| Findings, dry run | `./out/findings/*.jsonl` | files inside the container | `/var/lib/o11y-alerting/out/findings/*.jsonl` |
| Logs | stderr | App Insights via the host | journald, plus App Insights via OpenTelemetry |
| Run now | run the script | `POST /admin/functions/<name>` with the master key | `systemctl start o11y-alerting@<mode>.service` |
| Time limit | none | `functionTimeout` 30 min | `TimeoutStartSec` 30 min |
| Missed runs | n/a | not caught up (`run_on_startup=False`) | not caught up (`Persistent=false`) |
| Overlap | n/a | timer singleton | a timer never starts its unit while it runs |
| Extra platform pieces | none | Elastic Premium plan, host storage account, ACR, Key Vault | `python3.11`, `o11y` user, hardened oneshot unit |

The identity requirements differ only by what the platform itself needs. The pipeline's own calls are
the same everywhere:

```mermaid
flowchart LR
  uami["UAMI id-o11y-alerting<br/>granted by the IAM repo from identity/role-requirements.yaml"]
  subgraph app["Needed by the pipeline in every style"]
    rg["Resource Graph<br/>inventory: Reader on the MG"]
    mb["Metrics Batch API<br/>metrics: Monitoring Reader on the MG"]
    li["Logs Ingestion<br/>findings_ingest: Monitoring Metrics Publisher on the findings DCR"]
    lawq["LAW KQL, later<br/>law: Log Analytics Reader on the workspace"]
  end
  subgraph host["Needed only by the Function App host"]
    st["host storage<br/>host_storage: Storage Blob Data Owner on the storage account"]
    acr["image pull<br/>acr_pull: AcrPull on the ACR"]
    kv["Key Vault references<br/>secrets: Key Vault Secrets User on the vault"]
  end
  uami --> rg & mb & li & lawq
  uami -.-> st & acr & kv
  local["Local Mac: your az login user<br/>needs the same rows as the pipeline"] -.-> rg & mb & li
```

`findings_ingest` is scoped to the DCR that the deployment creates, so it is granted after the first
deploy in every style. The VM path skips `host_storage` and `acr_pull` and has no Key Vault today.

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
