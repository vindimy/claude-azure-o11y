# MVP design: VM CPU alerting + FinOps report, end to end

Date: 2026-09-15
Status: approved for implementation (autonomous session; assumptions listed in §11)
Scope: the "MVP" line of CLAUDE.md → Delivery plan. Nothing beyond VM `Percentage CPU`.

## 1. Goal

One scheduled Python Azure Function, shipped as a container, that for every subscription
under a management group (MG):

1. finds running VMs via Resource Graph,
2. pulls `Percentage CPU` for all of them through the Metrics Batch API (≤50 VMs per call),
3. raises an **Ops alert** to one Teams webhook when the 60‑minute average is hot,
4. writes a **FinOps Markdown report** of VMs whose 14‑day P95 CPU is cold, with a
   next‑size‑down recommendation and an estimated monthly saving,
5. is deployable from a clean shell by both `scripts/deploy.sh` and the Terraform module
   using the same `deploy.env` values and the same immutable `image_tag`, and
6. is built and pushed by GitLab CI.

Success = `run-once.sh` in dry‑run produces a correct report from recorded fixtures, unit tests
pass with ≥80 % coverage on `evaluate/` and `recommend/`, both deployment paths are complete
and parity‑tested, and a real run against one subscription alerts and uploads a report.

## 2. Corrections to CLAUDE.md discovered during research

These are facts verified against installed SDKs and Microsoft docs on 2026‑09‑15. CLAUDE.md
is updated in the same change so the two do not drift.

| Topic | CLAUDE.md says | Reality | Consequence |
|---|---|---|---|
| Batch metrics SDK | `azure-monitor-query` `MetricsClient` | `azure-monitor-query` 2.x removed `MetricsClient`; it lives in **`azure-monitor-querymetrics`** 1.0 (`azure.monitor.querymetrics.aio.MetricsClient`, `query_resources(...)`) | Dependency and import change only |
| Batch result → resource | implied 1:1 | `MetricsQueryResult` has **no `resource_id`**; the SDK drops the REST `resourceid` field | The thin client captures the raw JSON via `raw_response_hook` and maps `values[].resourceid` itself (see §5.3) |
| Function host storage | role matrix has only container‑scoped `Storage Blob Data Contributor` | The Functions host needs `AzureWebJobsStorage`. "No connection strings" forces the identity‑based form (`AzureWebJobsStorage__accountName` + `__credential=managedidentity` + `__clientId`), which requires **`Storage Blob Data Owner` on the storage account** | New row in `identity/role-requirements.yaml` and the CLAUDE.md matrix. This is a host requirement, not an app call, but it still has to be requested from the IAM repo |
| ACR pull by identity (script path) | `--acr-identity` flag | Site‑config properties `acrUseManagedIdentityCreds=true` and `acrUserManagedIdentityID=<UAMI resource id>` are what the platform reads. The script sets them with `az resource update` on `<site>/config/web`, which works on every az version | Script implementation detail |
| Parameter contract | table lacks the secret‑name parameters it says exist | — | Add `ops_webhook_secret_name` (required), `subscription_ids` (optional, fallback mode), `app_insights_name` (optional) to the table |

## 3. Non‑goals (MVP)

Memory/disk via LAW, every other resource type, email, owner/assignment‑group routing (tags are
*displayed*, not routed), per‑subscription deployment, Datadog, report hosting. `assignment-groups.yaml`
is still loaded and validated at startup so tag hygiene fails loudly from day one.

## 4. Runtime shape

```
function_app.py  (timer: %SCHEDULE_CRON%)
  └─ pipeline.run(settings, clients)              src/pipeline.py — the only orchestrator
       ├─ config.load()                           thresholds(default ⊕ mg) · ignore · routing · assignment-groups · vm-skus
       ├─ inventory.vms.list_vms()                Resource Graph, MG or subscription scope, paginated
       │    └─ filters: ignored RG · o11y-exclude tag · not running   (each skip logged + counted)
       ├─ group by (subscription, region) → chunks of 50
       ├─ metrics.batch.query()  ×2 per chunk     ops window (60 min, PT1M) and finops window (14 d, PT1H)
       │    asyncio.gather with Semaphore(MAX_CONCURRENCY)
       ├─ evaluate.vm.evaluate()                  pure; returns HotAlert | ColdFinding | Skip per VM
       ├─ evaluate.suppression                    blob-backed cache, 4 h window
       ├─ recommend.vm.recommend() + pricing      pure rule + Retail Prices lookup (cached per run)
       ├─ notify.teams.send()                     one Adaptive Card per un-suppressed HotAlert
       └─ notify.report.render() → sink.write()   reports/<date>/<mg>/virtual-machines.md
```

Everything is stateless except the suppression blob. One function instance handles the whole MG.

## 5. Components

### 5.1 `src/config/`

- `settings.py` — `Settings(BaseSettings)` from environment. Keys (all also app settings):
  `MG_ID`, `SUBSCRIPTION_IDS` (comma list; if set, scope is subscriptions and `MG_ID` is only
  the thresholds‑file name), `LAW_RESOURCE_ID` (accepted, unused in MVP), `STORAGE_ACCOUNT_NAME`,
  `REPORTS_CONTAINER=reports`, `SUPPRESSION_CONTAINER=suppression`, `OPS_TEAMS_WEBHOOK_URL`
  (Key Vault reference), `DRY_RUN=false`, `OUTPUT_DIR=./out`, `CONFIG_DIR=<repo>/config`,
  `MAX_CONCURRENCY=8`, `PRICING_CURRENCY=USD`, `AZURE_CLIENT_ID` (read by `DefaultAzureCredential`).
- `models.py` — pydantic v2 models for every YAML file. Unknown keys are errors
  (`extra="forbid"`), malformed emails and bad regexes fail at load.
- `loader.py` — `load_config(config_dir, mg_id) -> AppConfig`. Thresholds = deep‑merge of
  `thresholds/default.yaml` and `thresholds/<mg_id>.yaml` if present.

`config/thresholds/default.yaml` (shape):

```yaml
version: 1
tags:                       # no tag name is hard-coded anywhere else
  exclude: o11y-exclude
  threshold_prefix: o11y-threshold-     # o11y-threshold-cpu-hot=95 / o11y-threshold-cpu-cold=10
  owner: owner
  assignment_group: assignment_group
  car_id: car_id
windows:
  ops:    {lookback_minutes: 60, granularity: PT1M, aggregation: average}
  finops: {lookback_days: 14, granularity: PT1H, aggregation: average, percentile: 95, min_coverage: 0.5}
suppression_window_hours: 4
resource_types:
  vm:
    namespace: Microsoft.Compute/virtualMachines
    metrics:
      cpu: {metric_name: "Percentage CPU", ops_hot: 90, finops_cold: 20}
    recommend:
      min_vcpu: 1             # mg-prod.yaml overrides to 2
```

`config/ignore.yaml`, `config/routing.yaml`, `config/assignment-groups.yaml` as in CLAUDE.md.
`routing.yaml` maps the Ops webhook to an **environment variable name** (`ops_webhook_env:
OPS_TEAMS_WEBHOOK_URL`, optional `per_mg:` overrides) because each webhook is one app setting
holding one Key Vault reference; the secret *name* is a deployment parameter.

`config/vm-skus.yaml` — reference catalog, human‑maintained: `Standard_D2s_v5: {family: Dsv5,
vcpu: 2, memory_gib: 8}` … Family order is derived from `vcpu`. Unknown SKU → no
recommendation, row still reported with reason.

### 5.2 `src/inventory/vms.py`

`ResourceGraphInventory.list_vms(scope) -> list[VmResource]`. One KQL query, paginated with
`skip_token`, `top=1000`:

```kusto
resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, subscriptionId, resourceGroup, location, tags,
          vmSize = tostring(properties.hardwareProfile.vmSize),
          osType = tostring(properties.storageProfile.osDisk.osType),
          powerState = tostring(properties.extended.instanceView.powerState.code)
```

Filtering (ignore regex, exclude tag, `powerState != 'PowerState/running'`) happens in
`inventory/filters.py` (pure) so it is unit‑testable and skip reasons are explicit:
`ignored_rg`, `excluded_by_tag`, `not_running`.

### 5.3 `src/metrics/batch.py`

`MetricsBatchClient.query(region, subscription_id, resource_ids, namespace, metric_name,
window) -> dict[str, list[MetricPoint]]`, `MetricPoint(ts, average)`.

Implementation: one `azure.monitor.querymetrics.aio.MetricsClient` per region
(`https://{region}.metrics.monitor.azure.com`), `query_resources(...)` with a
`raw_response_hook` that stores the response JSON; the mapping is built from
`values[].resourceid` (lower‑cased for lookup) → `value[0].timeseries[0].data[]`. Resources
missing from the response get an empty list. HTTP 403 → `PermissionMissing("metrics")`.
API version is pinned by the SDK (`2023-10-01`); recorded in `docs/gotchas.md`.

### 5.4 `src/evaluate/`

- `percentile.py` — nearest‑rank percentile over floats; `None`s dropped.
- `vm.py` — `evaluate_vm(vm, ops_points, finops_points, cfg) -> VmEvaluation` where
  `VmEvaluation` carries `hot: HotAlert | None`, `cold: ColdFinding | None`, `skips: list[Skip]`.
  Rules: ops = mean of averages over the window ≥ `ops_hot` (tag override wins); finops =
  P95 < `finops_cold` **and** coverage ≥ `min_coverage` (points / expected points), else skip
  `insufficient_finops_data`. No points at all in ops window → skip `no_ops_data`.
- `suppression.py` — `SuppressionCache` (in memory) + `SuppressionStore` protocol with
  `BlobSuppressionStore` (one JSON blob per MG in the `suppression` container) and
  `LocalSuppressionStore` (file under `OUTPUT_DIR`). Key `"{resource_id}|{metric}"`, value
  last‑sent UTC. Entries older than the window are pruned on save. Suppression applies to
  Ops alerts only; the report always lists every finding.

### 5.5 `src/recommend/`

- `vm.py` — `recommend_vm(finding, catalog, rules) -> Recommendation`. Contract from CLAUDE.md:
  next SKU down in the same family, never cross families, never below `min_vcpu`. Confidence is
  `medium` in MVP (memory unknown) and the `reason` says so verbatim, e.g.
  `"P95 CPU 7.2% over 14d is below 20%. Memory not evaluated (MVP). Next smaller size in family Dsv5."`
  No‑op cases still return a `Recommendation` with `target_sku=None` and a reason
  (`already smallest allowed size`, `SKU not in catalog`).
- `pricing.py` — `RetailPriceClient.monthly_price(region, sku, os_type) -> Decimal | None`
  (httpx, unauthenticated, in‑run cache). Filter:
  `serviceName eq 'Virtual Machines' and armRegionName eq '{region}' and armSkuName eq '{sku}' and priceType eq 'Consumption'`;
  choose the item whose `productName` contains `Windows` iff `os_type == Windows`, skipping
  `skuName` containing `Spot` or `Low Priority`. Monthly = `unitPrice × 730`. Any failure →
  `None`; the row shows `n/a` and the run continues. Selection logic is a pure function with fixtures.

### 5.6 `src/notify/`

- `base.py` — `Notifier` protocol: `async send_hot(alert: HotAlert) -> None`;
  `ReportSink` protocol: `async write(path: str, markdown: str) -> str` (returns URL/path).
- `teams.py` — Power Automate Workflows webhook, Adaptive Card 1.4 payload
  (`{"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive", ...}]}`).
  Card facts: resource, subscription, RG, SKU, metric, observed vs threshold, `car_id`, `owner`,
  `assignment_group`, and a "Missing tags: …" line when any is absent. Card building is pure.
- `report.py` — `render_vm_report(findings, meta) -> str` (pure). Columns exactly as CLAUDE.md
  (resource, subscription, car_id, assignment_group, owner, current SKU, P95 CPU, recommended
  SKU, current monthly, projected monthly, saving) + footer: totals, excluded appendix (listed),
  ignored‑RG count, skip counts by reason, missing‑tag counts.
- `sinks.py` — `BlobReportSink` (`reports` container) and `LocalReportSink` (`OUTPUT_DIR`).
- `console.py` — dry‑run `Notifier` that logs the card JSON.

### 5.7 `src/pipeline.py` and `src/function_app.py`

`run(settings, config, inventory, metrics, pricing, notifier, sink, suppression) -> RunSummary`.
Fan‑out: `asyncio.gather` over chunk tasks guarded by a semaphore; a failed chunk is logged with
its subscription/region and counted, never fatal. `RunSummary` is logged as one JSON line.
`function_app.py` wires real clients from `Settings` and calls `run` from a timer trigger
`schedule="%SCHEDULE_CRON%"`. `src/run_local.py` does the same with `DRY_RUN=true`, local sinks,
and optional `SUBSCRIPTION_IDS`; `scripts/run-once.sh` wraps it.

### 5.8 Errors, permissions, logging

- `src/errors.py` — `PermissionMissing(need)` resolves role + scope text from
  `identity/role-requirements.yaml` (copied into the image) so the log line reads
  `missing Monitoring Reader on management group mg-prod (need: metrics)`.
- Thin clients translate 403 to `PermissionMissing`; everything else propagates as
  `HttpResponseError` and is caught per chunk.
- `src/logging_setup.py` — JSON lines on stdout (`ts, level, logger, msg, **extra`), picked up by
  the Functions host → App Insights when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set.

## 6. Identity

`identity/role-requirements.yaml` rows (id, role, scope_type, scope, purpose):
`inventory` Reader @ MG · `metrics` Monitoring Reader @ MG · `law` Log Analytics Reader @ LAW
(future, listed now) · `reports` and `suppression` Storage Blob Data Contributor @ container ·
`host_storage` **Storage Blob Data Owner @ storage account** (new) · `secrets` Key Vault Secrets
User @ KV · `acr_pull` AcrPull @ ACR. `scripts/gen-identity-doc.py` renders
`docs/identity-requirements.md`; `make identity-doc` runs it; a test asserts the doc is current.
`scripts/check-identity.sh` iterates the YAML and runs `az role assignment list` per row.

## 7. Container and deployment

- `Dockerfile`: `ARG BASE_IMAGE` (from `build/base-image.txt`), copy `src/`, `config/`,
  `identity/`, `host.json`, `requirements.txt` into `/home/site/wwwroot`, `pip install`.
  `build/base-image.txt` holds `<approved-registry>/azure-functions/python:4-python3.11`.
- App settings written by both paths (identical set):
  `FUNCTIONS_WORKER_RUNTIME=python`, `FUNCTIONS_EXTENSION_VERSION=~4`,
  `WEBSITES_ENABLE_APP_SERVICE_STORAGE=false`, `AzureWebJobsStorage__accountName`,
  `AzureWebJobsStorage__credential=managedidentity`, `AzureWebJobsStorage__clientId`,
  `AZURE_CLIENT_ID`, `MG_ID`, `SUBSCRIPTION_IDS`, `LAW_RESOURCE_ID`, `STORAGE_ACCOUNT_NAME`,
  `SCHEDULE_CRON`, `DRY_RUN`, `CONFIG_DIR=/home/site/wwwroot/config`,
  `OPS_TEAMS_WEBHOOK_URL=@Microsoft.KeyVault(VaultName=<kv>;SecretName=<ops_webhook_secret_name>)`,
  `APPLICATIONINSIGHTS_CONNECTION_STRING` (only when `app_insights_name` given).
  Key Vault references resolve through the UAMI (`keyVaultReferenceIdentity`).
- Path A `scripts/deploy.sh`: bash, `set -euo pipefail`, `--param-file` + flags, steps as in
  CLAUDE.md; ACR identity pull via `az resource update … --set
  properties.acrUseManagedIdentityCreds=true properties.acrUserManagedIdentityID=<uami id>`.
  `destroy.sh` deletes function app and plan; `--with-containers` also deletes the two containers.
- Path B `terraform/module-azure-o11y`: `azurerm_service_plan` (Linux, `plan_sku`),
  `azurerm_linux_function_app` with `identity { UserAssigned }`, `key_vault_reference_identity_id`,
  `storage_uses_managed_identity = true`, `site_config.container_registry_use_managed_identity`,
  `container_registry_managed_identity_client_id`, `application_stack.docker {…}`;
  two `azurerm_storage_container`. Data sources for RG, UAMI, storage, KV, ACR, optional App
  Insights. `terraform/examples/test-rg/` root with local backend.
- `.gitlab-ci.yml`: `lint` → `build` → `plan` → `apply` (manual). Terraform commands run inside
  `terraform/examples/test-rg`.
- `tests/test_param_parity.py` diffs `variables.tf`, `deploy.env.example`, `deploy.sh` flags and
  the CLAUDE.md table.

## 8. Testing

pytest, no live Azure. Fixtures in `tests/fixtures/`: Resource Graph page (2 pages), a batch
metrics response with three VMs (hot, cold, no‑data), Retail Prices response with Linux/Windows/
Spot items. Fake clients implement the same method signatures. Coverage gate 80 % on
`evaluate/` and `recommend/` via `--cov-fail-under` scoped in `pyproject.toml`. `ruff` and
`mypy --strict` clean on `src/`.

## 9. Repository additions

`pyproject.toml`, `requirements.txt`, `host.json`, `Makefile` (`test`, `lint`, `identity-doc`),
`.dockerignore`, `docs/gotchas.md`, `docs/identity-requirements.md`, `.env.example` for local runs.
Terraform, az, docker and func are **not installed** on the authoring workstation, so shell and
Terraform files are written and reviewed but only Python is executed locally; CI is the first
place `terraform validate` runs.

## 10. Order of work

1. Config models + loader + YAML files + tests.
2. Domain models, percentile, evaluate, suppression + tests.
3. Recommend + pricing selection + catalog + tests.
4. Report rendering + Teams card + tests.
5. Thin clients (RG, batch metrics, blob sinks, retail client) + fixture‑driven tests.
6. Pipeline + function_app + run_local + end‑to‑end dry‑run test.
7. Identity YAML, doc generator, errors, gotchas.
8. Dockerfile, host.json, requirements, CI.
9. deploy.sh, destroy.sh, deploy.env.example, build‑image.sh, check‑identity.sh, run‑once.sh.
10. Terraform module + example + parity test.
11. CLAUDE.md corrections, README.

## 11. Assumptions made without asking (autonomous session)

- Confidence is capped at `medium` for every VM recommendation until memory is evaluated.
- Suppression is included in the MVP because a 15‑minute schedule with a 60‑minute window
  would otherwise re‑alert four times per hour.
- One Adaptive Card per alert rather than one digest per run, so per‑owner routing later
  needs no card changes.
- Deallocated/stopped VMs are skipped (no CPU signal) and counted, not reported as cold.
- The thresholds file for an MG is named after `MG_ID` exactly.
- Host storage uses identity (`Storage Blob Data Owner` at account scope) rather than a key,
  because "no connection strings" outranks "container scope only".
