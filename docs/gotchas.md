# Gotchas

Things that behaved unexpectedly. Pin versions, note the date, link the fix.

## azure-monitor-query 2.x has no MetricsClient (2026-09-15)

`MetricsClient` (Metrics Batch API) moved to the `azure-monitor-querymetrics` package (1.0.0).
`azure-monitor-query` 2.x only ships `MonitorQueryLogsClient`. Dependency: `azure-monitor-querymetrics>=1.0,<2`.

## Batch metrics results carry no resource id (2026-09-15)

`MetricsQueryResult` drops the REST field `values[].resourceid`. `src/metrics/batch.py` captures the raw
JSON via `raw_response_hook` (the body is already loaded for non-streaming responses in the aiohttp
transport) and maps by lower-cased resource id; if the hook captures nothing it maps by request order and
logs a warning. API version pinned by the SDK: `2023-10-01`.

## Functions host storage needs Storage Blob Data Owner (2026-09-15)

Identity-based `AzureWebJobsStorage` (`__accountName`, `__credential=managedidentity`, `__clientId`) requires
`Storage Blob Data Owner` at storage-account scope on the UAMI. Container-scoped Contributor is not enough
for the host. Tracked as `host_storage` in `identity/role-requirements.yaml`.

## Elastic Premium + custom container: no webhook CD (2026-09-15)

Continuous deployment webhooks are unsupported on EP plans for containers. A deploy = change the image tag
(both paths do exactly that) and restart the app.

## pydantic does not treat `re.error` as a validation error (2026-09-15)

A `field_validator` that lets `re.compile` raise `re.error` produces a bare traceback, not a
`ValidationError`. `config/models.py` converts it to `ValueError` so a bad ignore regex fails startup cleanly.

## DCR stream types and table column types are spelled differently (2026-09-17)

DCR `streamDeclarations` use `datetime`; the Tables API (`Microsoft.OperationalInsights/workspaces/tables`)
uses `dateTime`, and the DCR has no `guid`. `schema/findings-tables.json` uses DCR spelling, and the
Terraform module and `deploy.sh` map `datetime` → `dateTime` for tables. Pinned API versions: tables
`2022-10-01`, DCE/DCR `2023-03-11`.

## Custom table PUT is asynchronous; the DCR needs the table first (2026-09-17)

A DCR whose `outputStream` is `Custom-<table>` is rejected until that table exists. `deploy.sh` polls the
table's `provisioningState` before creating the DCR; the module sets `depends_on`. The DCE and DCR must
also be in the workspace's region, which is read from the LAW itself (it may be in another subscription).

## Images built on Apple Silicon must target linux/amd64 (2026-09-17)

A plain `docker build` on an M-series Mac produces an arm64 image, and the Linux Functions host fails to
start it. `scripts/build-image.sh` always passes `--platform linux/amd64`.

## macOS bash 3.2: no `source <(...)` or associative arrays (2026-09-17)

`/bin/bash` on macOS is 3.2. There, `source <(cmd)` silently reads nothing and `declare -A` fails, so
`deploy.sh --param-file` loaded no parameters. Parameter parsing now lives in `scripts/lib/params.sh`
(a plain `read` loop and indexed arrays). Keep scripts 3.2-compatible.

## Logs Ingestion: new DCR role assignments and first rows are slow (2026-09-17)

After `Monitoring Metrics Publisher` is granted on the DCR, uploads can return 403 for up to about 30
minutes. The first rows in a new table can take several minutes to become queryable.
`azure-monitor-ingestion` 1.1 splits uploads into ≤1 MB gzip chunks and raises on the first failed chunk.

## systemd: RuntimeMaxSec does nothing for oneshot units; OnCalendar ANDs day and weekday (2026-09-21)

The VM runner (`o11y-alerting@.service`) is `Type=oneshot`, so its time limit is `TimeoutStartSec`, not
`RuntimeMaxSec`. NCRONTAB, like cron, ORs a restricted day-of-month with a restricted day-of-week;
`OnCalendar` ANDs them. The `ncrontab_to_oncalendar` filter rejects such expressions instead of
silently changing the schedule. Timers use `UTC` explicitly because NCRONTAB on Functions is UTC.

## UAMI is read by resource ID through azapi (2026-09-21)

`azurerm_user_assigned_identity` takes name + RG and only reads in the provider's subscription. The UAMI
comes from the IAM repo and may live elsewhere, so `module-azure-o11y` reads it with `data.azapi_resource`
by `uami_resource_id` (like the LAW), and the scripts use `az identity show --ids`. Pinned API version:
`Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31`.

## Subnet IP utilization from Resource Graph misses VMSS-uniform NICs (2026-09-22)

NICs on a uniform-orchestration VM scale set are not ARM resources, so they are absent from
`subnet.properties.ipConfigurations` and subnets backing uniform scale sets read low. The exact source
would be the per-VNET `usages` ARM call, rejected for now to keep inventory to one Resource Graph query
per type.

## Cosmos DB throughput metrics have a PT5M minimum grain (2026-09-22)

`ProvisionedThroughput` and `AutoscaleMaxThroughput` on `Microsoft.DocumentDB/databaseAccounts` reject
`PT1M` with a bad-request error that fails the whole batch call for the chunk. Both are input-only
metrics, so this binds **FinOps runs only**: the cosmos FinOps grain must be PT5M or coarser (it is
`PT1H`).

The `ops: PT5M` half of `granularity: {ops: PT5M, finops: PT1H}` is a choice, not a requirement — an
Ops run never requests those two metrics. It keeps both windows on the metrics' native grain:
`NormalizedRUConsumption` is reported per minute, the throughput inputs are not, and one grain per
type reads the same in both modes.
