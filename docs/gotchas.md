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

## Report path is per day, not per run

`reports/<yyyy-mm-dd>/<mg>/virtual-machines.md` is overwritten by each run that day (latest wins). Per CLAUDE.md.
