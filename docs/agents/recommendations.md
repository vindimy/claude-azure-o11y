# Recommendation rules

Read this before writing or changing a `src/recommend/<type>.py` module.

## Contract

- One module per resource type, bound to the type in `src/resource_types/<type>.py` (`SPEC.recommend`,
  `SPEC.rules_model`). Each is a rules engine (no ML) made of pure functions and unit-tested with fixtures.
- Input is a `ColdFinding`: the primary cold metric plus a `ColdObservation` per FinOps metric that had
  data (`finding.observation("memory")`) and the latest value of every input-only metric
  (`finding.inputs["provisioned"]`). A FinOps row exists only when every covered FinOps metric of the type
  is cold ([thresholds](thresholds.md#metric-fields)); a secondary metric without data lowers confidence.
- `target_sku` is a free string in the type's own spelling (`Standard_D4s_v5`, `S2`, `GP_Gen5_4`,
  `StandardPool 100`, `GP_Gen5 4 vCores`, `1200 RU/s`, `Standard 2 TU`); empty when there is no safe target.
- Every recommendation carries a `confidence` (`high|medium|low`) and a `reason` string. They are
  written verbatim to the `Confidence` and `Reason` columns of `O11yFinOpsFindings_CL`, so FinOps can see
  why each row exists.
- Per-MG knobs (e.g. `min_vcpu`) go in the `recommend:` block of the type in the thresholds config and
  are validated by the type's pydantic rules model at startup (unknown keys fail).
- Shared ladder helpers live in `recommend/ladder.py`: `next_smaller` (VM/PostgreSQL families) and
  `fit_up` (smallest size that covers `current × P95/100 × headroom`, used by SQL and Event Hubs).

## Pricing

`recommend/pricing.py` (`RetailPriceClient`) fills the cost columns from the Azure Retail Prices API
(`https://prices.azure.com/api/retail/prices`). The API is unauthenticated, so this is the one HTTP client
allowed outside the thin-client packages. Prices are cached per run, and a pricing failure leaves the cost
columns empty instead of failing the run. **Only VMs are priced today** (`SPEC.priced`); every other type
writes null cost columns and says so at the end of `Reason`. Pricing for SQL, PostgreSQL, Cosmos DB, and
Event Hubs is on the [roadmap](roadmap.md).

## Rules by type

Thresholds below are the defaults in `config/thresholds/default.yaml`; `headroom` defaults to 1.3.

- **VM** (`recommend/vm.py`, knob `min_vcpu`): P95 CPU < 20 % and, when the platform metric has data,
  P5 available memory > 70 % (peak use < 30 %). Next SKU down in the same family from
  `config/vm-skus.yaml`, never below `min_vcpu` (2 for prod MGs, `mg-prod.yaml`). Confidence `medium`
  with both metrics, `low` when memory had no data; the reason says which.
- **SQL Database** (`recommend/sqldb.py`, knobs `min_vcores`, `headroom`): the metric is
  `dtu_consumption_percent` for DTU tiers and `cpu_percent` for vCore tiers (`applies_to` on
  `purchasing_model`). DTU: the smallest service objective in the same tier whose DTUs cover
  `current × P95/100 × headroom` (`config/sql-skus.yaml`). vCore: the smallest ladder size that covers
  the same, not below `min_vcores`, keeping the SKU prefix (`GP_Gen5_8` → `GP_Gen5_4`). Databases in an
  elastic pool are skipped (`in_elastic_pool`); the pool gets the row. Serverless databases are Ops-only.
- **SQL Elastic Pool** (`recommend/sqlpool.py`): same rule over pool eDTU sizes / the pool vCore ladder.
- **SQL Managed Instance** (`recommend/sqlmi.py`, `min_vcores` default 4): `avg_cpu_percent` against the
  MI vCore ladder `[4, 8, 16, 24, 32, 40, 64, 80]`.
- **PostgreSQL Flexible Server** (`recommend/postgres.py`, `min_vcpu`): P95 CPU < 20 % and P95 memory
  < 30 %; next SKU down in the same family from `config/postgres-skus.yaml`.
- **Cosmos DB** (`recommend/cosmos.py`, knobs `min_ru`, `headroom`, `autoscale_ratio`): P95
  `NormalizedRUConsumption` (Maximum) < 30 %. Target = `max(min_ru, ceil(base × P95/100 × headroom))`
  rounded up to 100 RU/s, where `base` is the latest `AutoscaleMaxThroughput` (autoscale account → "lower
  the autoscale max") or `ProvisionedThroughput`. P95/median ≥ `autoscale_ratio` → recommend autoscale.
  Account-level metrics, so confidence is always `low` and the reason says "verify per container".
  Serverless accounts are Ops-only (`no_capacity_model`).
- **Event Hubs** (`recommend/eventhub.py`, `headroom`): P95 ingress < 30 % of capacity, where capacity
  is `sku.capacity × 1 MB/s` per TU (Basic/Standard) or `× 5 MB/s` per PU (Premium, conservative end of
  Azure's 5–10 MB/s). Target units = `max(1, ceil(capacity × P95/100 × headroom))`. Standard→Basic is
  never recommended (needs capture, consumer-group, and retention checks). Dedicated is Ops-only.
- **VNET subnets:** Ops-only (capacity, not cost).
