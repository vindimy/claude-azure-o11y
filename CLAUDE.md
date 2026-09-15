# CLAUDE.md — Azure Resource Utilization Alerting (Ops + FinOps)

## What this is

A scheduled Python Azure Function that iterates every subscription under a configured
Azure management group (MG), evaluates built-in Azure Monitor metrics for high-visibility
resource types, and produces two outputs:

1. **Ops alerts** — resources running hot on CPU / memory / disk (near-real-time, actionable).
2. **FinOps findings** — provisioned-but-underutilized resources with a concrete
   downsizing recommendation and an estimated monthly saving, published as a per-resource-type
   Markdown savings report.

Why it exists: Diagnostic Settings are **not** shipped at scale and there is no centralized
metric store across subscriptions. This function fills that gap by querying each resource's
built-in platform metrics directly. Azure Advisor was rejected on purpose: too generic, no
Event Hub / VNET coverage, no ownership-based routing, no control over thresholds.
If someone asks "why not Advisor?", that is the answer.

Owner: Dmitriy (Principal Cloud Engineer, Cloud Engineering). Customers: Ops team, FinOps team,
and resource-owning app teams (via tag routing).

## Scope

### Resource types (in priority order)

| # | Type | Ops signal (hot) | FinOps signal (cold) | Metric source |
|---|------|------------------|----------------------|---------------|
| 1 | Virtual Machines (**MVP: CPU only**) | CPU, memory, disk space | Low CPU/memory → smaller SKU in same family | Platform: `Percentage CPU`. Guest: AMA→central LAW (`InsightsMetrics` / `Perf`) for memory + disk |
| 2 | Azure SQL Database / Elastic Pool | CPU %, DTU/vCore %, storage % | Low DTU/vCore → lower tier | Platform metrics |
| 3 | Azure SQL Managed Instance | CPU %, storage % | Low vCore usage → fewer vCores | Platform metrics |
| 4 | PostgreSQL Flexible Server | CPU, memory, storage % | Low CPU/memory → smaller compute tier | Platform metrics |
| 5 | Cosmos DB | Normalized RU consumption, storage | Low normalized RU → lower provisioned RU or autoscale | Platform metrics (`NormalizedRUConsumption`, `ProvisionedThroughput`) |
| 6 | Event Hubs Namespace | Throttled requests, CPU (Premium/Dedicated) | Low incoming bytes/msgs vs TU/PU/CU → lower tier / fewer units | Platform metrics |
| 7 | Virtual Networks | Subnet IP utilization ≥ threshold | (none — this is capacity, not cost) | **ARM math via Resource Graph**, not Monitor: address space minus allocated IPs and Azure-reserved (5/subnet) |

Future (backlog, not now): AKS, App Service Plans, Storage, Redis, Function Apps.

### Out of scope

- Persisting evaluations or history. Every run is stateless. Do not add a database/table.
- Auto-remediation. This system recommends; humans resize.
- Azure Advisor / Cost Management integration.
- Guest-OS metrics for anything other than VMs.

## Architecture

```
Timer trigger (cron per MG config)
  └─ Inventory: Azure Resource Graph, one query per resource type, MG-scoped
       └─ Group by (subscription, region, resource type)
            └─ Metrics: Azure Monitor **metrics:getBatch** (regional endpoint), ≤50 resource IDs per call
                 └─ Evaluate against threshold set for this MG
                      ├─ Ops hot → Teams (Ops channel + owner channel by tag) + SMTP email
                      └─ FinOps cold → recommendation + savings estimate → Markdown report
```

### Avoiding N×M metric calls (decision)

Do **not** call `Microsoft.Insights/metrics` per resource. Use the Azure Monitor
**Metrics Batch API** (`https://{region}.metrics.monitor.azure.com/subscriptions/{subId}/metrics:getBatch`)
via `azure-monitor-querymetrics` `MetricsClient.query_resources()` (`azure-monitor-query` 2.x no longer
ships the metrics client; see `docs/gotchas.md`). Constraints Claude Code must respect:

- One call = one resource type + one region + one subscription, up to **50 resource IDs**.
- Resource Graph provides inventory (type, region, SKU, tags) so batches can be built without
  listing resources per subscription via ARM.
- Guest VM metrics (memory, disk) come from **one** KQL query against the central LAW,
  filtered to the resource IDs in scope, not per-VM queries.
- VNET utilization comes from Resource Graph only; no Monitor calls.
- Expect 30–50 subscriptions per MG. Design for one function instance handling a full MG
  in a single run with async fan-out per subscription (`asyncio` + semaphore). Fallback of
  last resort: deploy one function per subscription — keep `MG_ID` vs `SUBSCRIPTION_IDS`
  scoping switchable via config so this needs no code change.

### Identity

- Runtime identity: one **user-assigned managed identity (UAMI)**, created and managed
  **externally** by the bank's IAM automation repo — this repo cannot create identities or role
  assignments (locked-down Azure IAM). Both deployment paths receive the UAMI **name** as a
  parameter and only *attach* it to the Function App.
- This repo is nonetheless the **source of truth for what the UAMI needs**:
  `identity/role-requirements.yaml` (machine-readable: role, scope type, scope expression,
  purpose) plus the human table below. The YAML is what gets handed to the external IAM repo
  as the request; `docs/identity-requirements.md` is generated from it (`make identity-doc`).
  Any new Azure call that needs a permission must first add a row here — code review rejects
  PRs where the two drift.
- Role matrix (least privilege — one role per need, narrowest scope that works):

  | Need | Role | Scope |
  |------|------|-------|
  | Inventory via Resource Graph, read SKUs/tags/subnets | `Reader` | Management group |
  | Platform metrics via Metrics Batch API | `Monitoring Reader` | Management group |
  | VM guest metrics (memory, disk) via KQL | `Log Analytics Reader` | Central LAW resource only |
  | Write reports + suppression cache | `Storage Blob Data Contributor` | The `reports` and `suppression` containers only (container-scope, not account) |
  | Functions host storage (`AzureWebJobsStorage` over identity; timer leases, host state) | `Storage Blob Data Owner` | The storage account (host requirement, not an app call; keys/connection strings are prohibited so identity is the only option) |
  | Read webhook URLs / SMTP creds | `Key Vault Secrets User` | The Key Vault (or per-secret if the bank's KV model allows) |
  | Pricing (Retail Prices API) | none — public, unauthenticated | — |
  | Pull the function image | `AcrPull` | The ACR resource only |
  | Datadog / other outside processes (future) | none on Azure side; API keys live in Key Vault | — |

  Pushing images is the GitLab runner's job, not the UAMI's; the runner already has push access
  to the ACR and that grant is outside this repo.

  Anything outside this table (e.g. Cost Management reader for actuals) requires an ADR, a new
  row in `identity/role-requirements.yaml`, and a request to the IAM repo before code is written.
- Startup self-check: on first run (and `scripts/check-identity.sh`), verify each required
  permission with a cheap read call and log which rows are missing, so a half-provisioned UAMI
  fails with "missing Log Analytics Reader on <LAW id>" instead of a generic 403 deep in a loop.
- `DefaultAzureCredential` with `AZURE_CLIENT_ID` set to the UAMI client ID. Locally, `az login`.
- Secrets (SMTP creds, Power Automate webhook URLs) live in the pre-existing Key Vault,
  referenced via Function App Key Vault references — never in app settings as plaintext,
  never in Terraform state as literals.

### Thresholds and noise control

- Config-driven: `config/thresholds/<mg-name>.yaml`, one file per MG, with a `default.yaml`
  fallback. Structure: per resource type → per metric → `ops_hot`, `finops_cold`, `lookback`,
  `aggregation`, `percentile`.
- Defaults: FinOps lookback **14 days, P95**; Ops lookback **60 min, average** (configurable
  down to 15 min).
- Tag overrides: a resource tagged `o11y-threshold-cpu-hot=95` overrides the config for that
  metric. Tag `o11y-exclude=true` skips the resource entirely (must appear in the report's
  "excluded" appendix so exclusions are visible, not silent).
- **Resource group ignore list:** `config/ignore.yaml` holds a list of regex patterns
  (Python `re`, case-insensitive, full-match) applied to resource group names, plus an optional
  per-MG section. Matching RGs are dropped at the Resource Graph inventory stage — before any
  metrics call — so ignored resources cost nothing. Example:

  ```yaml
  resource_groups:
    - '^rg-.*-dev(-.*)?$'
    - '^rg-sandbox-.*'
  per_mg:
    mg-nonprod:
      - '.*-poc-.*'
  ```
  Ignored RGs are counted (not listed) in the report footer. Patterns must be tested in
  `tests/test_ignore.py` with positive and negative cases; a bad regex fails startup, not silently
  matches everything.
- Dedup/suppression: an Ops alert for the same (resource, metric) is not re-sent within
  `suppression_window` (default 4h). Since nothing is persisted, implement this with a
  small in-memory/blob-backed cache in the existing storage account (`suppression/` container,
  short TTL) — this is the one permitted use of storage.

### Notifications

- **Ownership tags (fixed names, used for routing and reporting):**

  | Tag | Meaning | Used for |
  |-----|---------|----------|
  | `owner` | Email address of the resource owner | Direct email notification (validate as email; if malformed, treat as missing) |
  | `assignment_group` | Name of the owning team | Looked up in `config/assignment-groups.yaml` → team email and optional Teams webhook |
  | `car_id` | Numeric application identifier | Displayed in every alert and report row; grouping key in the report; no routing on its own |

  `config/assignment-groups.yaml` is the mapping file (`<assignment_group>: {email: ..., teams_webhook: ...}`),
  maintained by humans, validated on startup (unknown keys/malformed emails fail loudly).
  Routing order per alert: Ops channel always; then `owner` email if present; then
  `assignment_group` email/webhook if mapped. Missing or unmapped values → Ops channel only, and
  the alert body plus report footer say exactly which tag was missing/unmapped so tag hygiene
  becomes visible. Never guess an owner.
- **Teams:** Power Automate "Workflows" incoming-webhook (Adaptive Card payload). Do not use
  Office 365 connectors (retired). Webhook URLs live in Key Vault; `config/routing.yaml` holds
  the Ops webhook secret name and any per-MG overrides.
- **Email:** SMTP relay (assumed; bank-specific relay TBD — keep the sender behind an
  interface so swapping to Logic App / ACS is a one-file change).
- **FinOps report:** Markdown, one file per resource type per run:
  `reports/<yyyy-mm-dd>/<mg-name>/<resource-type>.md`, uploaded to a `reports` container
  in the existing storage account. Each row: resource, subscription, car_id, assignment_group, owner, current SKU,
  observed P95 utilization, recommended SKU/units, current monthly cost, projected monthly
  cost, estimated saving. Footer: totals + excluded resources appendix.
  Pricing via the Azure Retail Prices API (`https://prices.azure.com/api/retail/prices`),
  unauthenticated, cached per run.

### Recommendation rules (rules engine, not ML)

`src/recommend/<type>.py`, one module per resource type, pure functions, unit-tested with
fixtures. Examples of the contract:

- VM: if P95 CPU < 20% and P95 memory < 30% → next SKU down in the same family; never cross
  families; never recommend below 2 vCPU for prod MGs.
- Cosmos: if P95 normalized RU < 30% → recommend `max(400, ceil(P95 * provisioned / 100) * 1.3)`
  rounded to 100 RU, or autoscale if variance is high.
- Event Hub: if P95 incoming bytes/sec < 30% of tier capacity → fewer TU/PU, or Standard→Basic
  if features allow (check capture, consumer groups, retention before suggesting).

Every recommendation carries a `confidence` (`high|medium|low`) and a `reason` string that
appears verbatim in the report. FinOps should never have to guess why a row exists.

## Repository layout

```
.
├── CLAUDE.md
├── README.md
├── Dockerfile                   # approved base image + src/; built only by CI (or build-image.sh)
├── build/base-image.txt         # pinned approved base image reference
├── src/
│   ├── function_app.py          # timer trigger entry point (Python v2 programming model)
│   ├── bootstrap.py             # wires real / dry-run clients from Settings, runs the pipeline
│   ├── pipeline.py              # orchestrator: inventory → batch metrics → evaluate → notify/report
│   ├── models.py                # shared frozen dataclasses; ports.py holds the client Protocols
│   ├── storage/                 # blob thin client (reports, suppression)
│   ├── inventory/               # Resource Graph queries, one per resource type
│   ├── metrics/                 # batch metrics client, LAW KQL client, VNET calculator
│   ├── evaluate/                # threshold evaluation, suppression
│   ├── recommend/               # per-type downsizing rules + pricing
│   ├── notify/                  # teams.py, email.py, report.py (interface: Notifier)
│   └── config/                  # loader + pydantic models for thresholds/routing
├── config/
│   ├── thresholds/default.yaml
│   ├── thresholds/<mg-name>.yaml
│   ├── routing.yaml             # Ops webhook secret name, per-MG overrides
│   ├── assignment-groups.yaml   # assignment_group → team email / webhook
│   ├── vm-skus.yaml             # VM SKU catalog (family, vCPU, memory) used for downsizing
│   └── ignore.yaml              # regex RG ignore list
├── tests/                       # pytest; fixtures = recorded API responses, no live Azure
├── terraform/
│   └── module-azure-o11y/
│       ├── data.tf              # existing RG, UAMI (by name), storage account, key vault
│       ├── locals.tf
│       ├── variables.tf
│       ├── main.tf              # app service plan, function app, app settings, KV references
│       └── outputs.tf
├── identity/
│   └── role-requirements.yaml   # roles + scopes the external IAM repo must grant the UAMI
├── scripts/
│   ├── deploy.sh                # self-contained az-CLI deployment of the whole stack (no Terraform)
│   ├── destroy.sh
│   ├── deploy.env.example       # parameter file consumed by deploy.sh
│   ├── build-image.sh           # dev-only local build/push of a dev tag to ACR
│   ├── check-identity.sh        # verifies the UAMI has every row in role-requirements.yaml
│   └── run-once.sh              # invoke the function locally against one subscription
└── .gitlab-ci.yml               # validate → plan → manual approve → apply
```

## Conventions

- Python 3.11+, Azure Functions Python **v2** model, `azure-identity`, `azure-monitor-querymetrics`,
  `azure-mgmt-resourcegraph`, `pydantic` v2, `httpx` (async). No `azure-cli` calls from code.
- Type hints everywhere; `ruff` + `mypy` clean; `pytest` with ≥ 80% coverage on
  `evaluate/` and `recommend/` (these are the parts that can embarrass us).
- All Azure API calls go through thin clients in `metrics/`, `inventory/` and `storage/` so tests can
  swap them. No SDK calls inside `evaluate/`, `recommend/`, or `notify/`.
- Structured JSON logging to App Insights; log every skipped resource with a reason.
- Terraform: latest Terraform and `azurerm` provider (4.x). Module name `module-azure-o11y`.
  Separate `data.tf`, `locals.tf`, `variables.tf`, `main.tf`, `outputs.tf`. No identity or
  role-assignment resources anywhere in the module. Pre-existing inputs, passed by name:
  resource group, UAMI, storage account, Key Vault.
  Naming and tagging standards will be supplied later — keep names in `locals.tf` only so
  they can be swapped in one place.
- Config over code: no threshold, webhook, tag name, MG ID, or LAW ID is hard-coded.
- Managed identity everywhere; no keys, no SAS, no connection strings in settings.

## Deployment — two self-contained paths, same parameters

The function runs as a **custom Linux container** (the established US Bank pattern). Code ships
as an image: GitLab CI builds it from an approved base image plus this repo's source and pushes
it to the configured ACR; **both** deployment paths then only *reference* an image tag. No path
builds or pushes images, no path uses Kudu/zip deploy, and neither path needs a Docker daemon.

Both paths deploy the **entire stack** (Elastic Premium plan, Function App with UAMI attached and
the container image configured, app settings with Key Vault references, storage containers) into
a given resource group. Neither creates the RG, UAMI, storage account, Key Vault, or ACR — those
are inputs. Neither depends on the other: the shell path must not shell out to Terraform, and the
module must not call the script.

### Container image

- `Dockerfile` at repo root, `FROM <approved-registry>/azure-functions/python:4-python3.11`
  (exact approved source pinned in `build/base-image.txt`; never pull from Docker Hub/MCR directly).
- Image contains `src/`, `requirements.txt` installed at build time, `host.json`; no secrets,
  no config files with environment values — everything comes from app settings at runtime.
- Tag = `$CI_COMMIT_SHA` (immutable) plus a moving `main` tag for convenience; deployments always
  pin the SHA tag. Image name: `o11y-alerting`. Full ref: `<acr>.azurecr.io/o11y-alerting:<sha>`.
- Local build for development: `scripts/build-image.sh` (same Dockerfile, same base) so
  `deploy.sh` can push a dev tag from a workstation with ACR access.

### Shared parameter contract

Identical names and meanings in both paths (snake_case in Terraform, UPPER_SNAKE in the script):

| Parameter | Example | Notes |
|-----------|---------|-------|
| `resource_group_name` | `rg-o11y-test` | existing |
| `location` | `centralus` | |
| `uami_name` | `id-o11y-alerting` | existing, external IAM repo; resolved to ID + client ID |
| `storage_account_name` | `sto11yalerting` | existing; containers `reports`, `suppression` created if missing |
| `key_vault_name` | `kv-o11y-alerting` | existing; secret names for webhooks/SMTP are parameters too |
| `management_group_id` | `mg-prod` | scope of iteration |
| `law_resource_id` | `/subscriptions/…/workspaces/law-central` | central LAW for VM guest metrics |
| `acr_name` | `acrusbcloud` | existing; UAMI needs `AcrPull` |
| `image_name` | `o11y-alerting` | |
| `image_tag` | `a1b2c3d` | immutable SHA tag built by CI; **required**, no default |
| `function_app_name`, `app_service_plan_name` | | naming standards TBD; defaults in `locals.tf` / script |
| `plan_sku` | `EP1` | custom containers need Elastic Premium / Dedicated; Consumption is not valid |
| `schedule_cron` | `0 */15 * * * *` | NCRONTAB |
| `dry_run` | `false` | |
| `ops_webhook_secret_name` | `o11y-ops-teams-webhook` | Key Vault secret name holding the Ops Teams webhook URL; **required** |
| `subscription_ids` | `` | optional comma list; when set, scope is these subscriptions instead of the MG (fallback mode) |
| `app_insights_name` | `` | optional existing App Insights component in the RG; sets `APPLICATIONINSIGHTS_CONNECTION_STRING` |

Adding a parameter means adding it to `variables.tf`, `deploy.env.example`, the script's arg
parser, **and** this table in the same PR. `tests/test_param_parity.py` diffs the three.

### Path A — `scripts/deploy.sh` (development, fast iteration)

- Pure `az` CLI, idempotent (re-runnable without destroying anything).
- Parameters from `--param-file deploy.env` (KEY=VALUE) or individual flags; flags override file.
  `deploy.env.example` documents every key with a comment.
- Steps: validate inputs → verify `image_tag` exists in ACR (`az acr repository show-tags`) →
  resolve UAMI/storage/KV IDs → create/update EP1 plan + function app with
  `--image <acr>/<image>:<tag>` and `--acr-identity <uami>` → assign UAMI → set app settings
  (KV references) → ensure containers → run `check-identity.sh` → print invoke URL and a
  manual-trigger command. Re-running with a new `image_tag` is the deploy.
- `destroy.sh` removes only what `deploy.sh` created (plan, function app, containers on request).

### Path B — `terraform/module-azure-o11y` (CI, prod)

- Module exposes the same contract as `variables.tf`; a root example in
  `terraform/examples/test-rg/` calls it end-to-end with a local backend for validation.
- `azurerm_linux_function_app` with `site_config.application_stack.docker { registry_url,
  image_name, image_tag }`, `container_registry_use_managed_identity = true`,
  `container_registry_managed_identity_client_id = <uami client id>`. No admin credentials,
  no `DOCKER_REGISTRY_SERVER_*` secrets in app settings.
- A new `image_tag` is a plan diff on one attribute — that *is* the code deploy. Terraform never
  builds, pushes, or zips anything; `archive_file`, `zip_deploy_file`, the `docker` provider,
  and `local-exec` are all off-limits in this module.

### Running locally

`scripts/run-once.sh` runs the evaluation locally (`func start` or direct Python entry) against a
single subscription with `DRY_RUN=true` (no notifications, report written to `./out/`).

## GitLab CI

Stages: `lint` (ruff, mypy, pytest, `terraform fmt -check`, `terraform validate`) →
`build` (`docker build` from the approved base image + local source, `docker push` to ACR as
`$CI_COMMIT_SHA`; the runner already has push access) → `plan` (`-var image_tag=$CI_COMMIT_SHA`,
artifact: plan file) → `apply` (manual, protected branch only, consumes the plan artifact).
Secrets via GitLab CI variables; the pipeline authenticates with the Terraform SP, not the UAMI.
`build` must succeed before `plan` so a plan never references a tag that doesn't exist.

## Delivery plan

**MVP (do this first, end-to-end, before touching other types):**
VM CPU only. Resource Graph inventory → batch metrics → thresholds from `default.yaml` →
Ops alert to one Teams webhook → FinOps Markdown report for VMs uploaded to storage →
CI builds and pushes the image; both `scripts/deploy.sh` and the Terraform module deploy it
from a clean shell with the same `deploy.env` and the same `image_tag`.

Then, one resource type per iteration, each with its own `recommend/` module and tests:
VM memory/disk (LAW) → Cosmos → SQL DB → SQL MI → PostgreSQL → Event Hub → VNET.

**Backlog (research tasks, produce a short ADR in `docs/adr/` before implementing):**

- Report hosting for outside teams: evaluate (a) static website on the storage account with
  Markdown rendered to HTML at publish time + private endpoint, (b) Teams channel file upload
  via Graph with a link in the alert, (c) GitLab Pages / wiki. Pick the one with the fewest
  new permissions.
- Email transport: confirm the bank's SMTP relay; otherwise Logic App or ACS Email.
- Datadog integration (both Ops and FinOps alerts): evaluate Datadog Events API v2 and Logs
  intake via API key in Key Vault vs. a webhook integration; goal is to forward only alerts
  that pass a per-MG "send to Datadog" filter. Note the existing Datadog Azure integration
  already pulls Azure Monitor metrics — clarify with Enterprise Observability where the line is
  so this does not become a turf issue.
- Per-subscription deployment mode (fallback if MG-scope performance or permissions fail).

## Working with Claude Code on this repo

- Read this file, then `config/thresholds/default.yaml`, before proposing changes.
- Prefer small, reviewable PRs: one resource type or one notifier per PR.
- When adding a resource type: inventory query → metric definitions → threshold defaults →
  recommend module → tests → report section, in that order. Do not skip tests.
- Never add an identity or role-assignment resource, a database, or a hard-coded
  threshold/tag name/webhook. New permissions go in `identity/role-requirements.yaml` first. If it seems necessary, stop and explain why in the PR
  description instead.
- When an Azure API behaves unexpectedly, pin the API version explicitly and note it in
  `docs/gotchas.md`.

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues for `vindimy/claude-azure-o11y` via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary, label strings equal to role names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` at the repo root and ADRs in `docs/adr/`. See `docs/agents/domain.md`.
