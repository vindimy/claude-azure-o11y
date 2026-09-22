# Design: platform metrics for the common resource types

Date: 2026-09-22
Status: approved for implementation (autonomous session; assumptions listed in §12)
Scope: rows 1–7 of the resource-type table in `docs/agents/architecture.md`, using Azure Monitor
platform metrics only. VM guest-OS metrics (AMA/DCR → LAW) stay out: they are collected elsewhere.

## 1. Goal

After this work the pipeline evaluates, per management group and per run mode:

| Type | Config key | Ops (hot) | FinOps (cold → recommendation) |
|---|---|---|---|
| Virtual Machines | `vm` | CPU, available memory, OS-disk IOPS/bandwidth %, uncached VM IOPS/bandwidth % | CPU + memory → smaller SKU in the family (existing rule, memory clause now live) |
| Azure SQL Database | `sqldb` | DTU % or CPU % (by purchasing model), storage %, workers % | DTU/CPU → lower service objective / fewer vCores |
| Azure SQL Elastic Pool | `sqlpool` | eDTU % or CPU %, storage % | eDTU/CPU → smaller pool size |
| Azure SQL Managed Instance | `sqlmi` | CPU %, storage % (derived) | CPU → fewer vCores |
| PostgreSQL Flexible Server | `postgres` | CPU, memory, storage %, disk IOPS % | CPU + memory → smaller compute SKU in the family |
| Cosmos DB account | `cosmos` | normalized RU %, throttled request % | normalized RU → lower provisioned RU/s, or autoscale |
| Virtual Network subnets | `vnet` | subnet IP utilization % (Resource Graph math) | none |

Success = every type produces rows from recorded fixtures in dry run, the existing VM CPU
behaviour is unchanged for VMs with no memory data, `make test` stays ≥ 80 % on `evaluate/` and
`recommend/`, `make lint` passes, and no new permission is needed (Reader + Monitoring Reader on the
MG already cover every call below).

## 2. What the MVP cannot express

The MVP is typed on `VmResource` end to end and assumes one metric, one aggregation, "high is hot",
and one namespace per run. The new types break each of those:

1. **Direction.** VM `Available Memory Percentage` is hot when *low*.
2. **Aggregation per metric.** Cosmos `NormalizedRUConsumption` is meaningful as `Maximum`; Event
   Hubs counts are `Total`; everything else is `Average`.
3. **Window reduction.** A throttling count needs the *sum* over the Ops window, not the mean.
4. **Applicability.** A SQL database emits `dtu_consumption_percent` or `cpu_percent`, never both
   meaningfully; which one depends on inventory (`sku.tier`).
5. **Derived metrics.** SQL MI storage % is `storage_space_used_mb / reserved_storage_mb`; Event
   Hubs ingress % is `IncomingBytes` against the namespace's throughput-unit capacity.
6. **Granularity per type.** Cosmos `ProvisionedThroughput` has a PT5M minimum grain.
7. **No metric at all.** Subnet utilization comes from Resource Graph arithmetic.
8. **Several metrics per resource in FinOps.** The VM rule needs CPU *and* memory.

## 3. Architecture

The pipeline becomes a loop over a **resource-type registry**. Everything type-specific lives in one
`ResourceTypeSpec` per type; the pipeline, evaluators, and row builders are type-agnostic.

```
pipeline.run(mode)
  for each enabled type (config order, filtered by RESOURCE_TYPES):
    inventory.list_resources(kind, scope)      Resource Graph, one KQL per type (inventory/<type>.py)
    filters.apply(resources, tags, ignore, spec.active)   ignored RG · o11y-exclude · type "active" check
    metrics for this mode:
      spec.metric_source == "monitor":
        group by (subscription, region) → chunks of BATCH_SIZE
        metrics.query(region, sub, ids, namespace, [MetricRequest(name, aggregation)…], window)
          one getBatch call per chunk carrying every metric this mode needs for this type
      spec.metric_source == "inventory":
        one synthetic point per metric from resource.props (vnet)
    derive.apply(spec, resource, series)       derived metrics from raw series + props
    evaluate:
      ops:    evaluate_hot per applicable metric with ops_hot     → one row per (resource, metric)
      finops: evaluate_cold per applicable metric with finops_cold → ColdObservation per metric
              spec.recommend(resource, observations, rules, catalogs) → Recommendation → one row per resource
    rows → FindingsSink (one write per run, as today)
```

Run summary counters gain a per-type breakdown (`by_type: {vm: {...}}`) but the log line and the
tables keep every existing field.

### 3.1 Modules

| Module | Change |
|---|---|
| `src/models.py` | `VmResource` → generic `Resource` (§4). `MetricPoint.average` → `MetricPoint.value`. `HotAlert`/`ColdFinding` reference `Resource`. New `ColdObservation`, `MetricRequest`. |
| `src/resource_types/` | **new.** `registry.py` with `ResourceTypeSpec` and `TYPES: dict[str, ResourceTypeSpec]`; one `<type>.py` per type holding its spec (inventory query + parser, active check, derive functions, recommender binding). |
| `src/inventory/graph.py` | **new.** `ResourceGraphInventory.list_resources(kind, scope)`: runs `TYPES[kind].query`, pages, maps rows through `TYPES[kind].parse`. The only module importing `azure.mgmt.resourcegraph`. `inventory/vms.py` becomes the VM query + parser only (no SDK). |
| `src/inventory/filters.py` | `filter_resources(resources, tags, patterns, active)`; the VM "running" check moves into the VM spec's `active` callable. |
| `src/metrics/batch.py` | `query(..., metrics: list[MetricRequest], window)` requests all names in one call with the union of aggregations; returns `dict[resource_id, dict[metric_name, list[MetricPoint]]]`, each series holding the value for *its* aggregation. |
| `src/metrics/derive.py` | **new, pure.** Named derivations: `ratio_percent`, `bytes_per_second_percent`. Selected by `derive:` in config, with `inputs:` naming the raw metrics. |
| `src/evaluate/metric.py` | **new, pure** (replaces `evaluate/vm.py`). `evaluate_hot(resource, series, spec, window_cfg, metric_key, tags)` and `evaluate_cold(...)` for any metric, honouring `hot_when`, `reduce`, and the percentile flip (§5). |
| `src/recommend/` | `vm.py` gains the memory clause. New `sqldb.py`, `sqlpool.py`, `sqlmi.py`, `postgres.py`, `cosmos.py`, `eventhub.py`. Shared `ladder.py` (next-smaller in an ordered list; ceil-to-ladder). |
| `src/notify/findings.py` | Row builders take `Resource`; `OsType` comes from `props`; `MetricContext` built per (type, metric). No schema change. |
| `src/config/models.py` | `ResourceTypes` becomes `dict[str, ResourceTypeThresholds]` (§6). New catalogs: `sql-skus.yaml`, `postgres-skus.yaml`. |
| `src/pipeline.py` | The loop above. |
| `src/ports.py` | `InventoryPort.list_resources(kind, scope)`; `MetricsPort.query(...)` new signature. `PricingPort` unchanged. |

Code boundaries from `architecture.md` hold: SDK imports stay in `inventory/graph.py`,
`metrics/batch.py`, `storage/law.py`; `evaluate/`, `recommend/`, `notify/`, `resource_types/` are
SDK-free and take plain data.

## 4. Domain model

```python
@dataclass(frozen=True)
class Resource:
    kind: str                 # registry key: vm, sqldb, …
    id: str                   # ARM id (for vnet: the subnet id)
    name: str                 # for vnet: "<vnet>/<subnet>"
    type: str                 # ARM type, lower case; written to ResourceType
    subscription_id: str
    resource_group: str
    location: str
    sku: str                  # written to the Sku column; type-specific spelling (§8)
    tags: dict[str, str]
    props: dict[str, str | int | float | bool]   # type-specific facts used by applies_to, derive, recommend

@dataclass(frozen=True)
class MetricPoint:
    timestamp: datetime
    value: float | None       # the metric's configured aggregation

@dataclass(frozen=True)
class MetricRequest:
    name: str
    aggregation: str          # Average | Maximum | Minimum | Total | Count

@dataclass(frozen=True)
class ColdObservation:        # one per FinOps metric, cold or not
    metric: str
    percentile: int           # 95, or 5 for hot_when=below
    value: float
    threshold: float
    cold: bool
    coverage: float
    threshold_source: ThresholdSource
```

`HotAlert` keeps its fields (`resource: Resource`). `ColdFinding` keeps its fields and gains
`observations: tuple[ColdObservation, ...]` so the recommender and the `Reason` string see every
metric; the row's `MetricName`/`ObservedValue`/`Percentile` come from the **primary** metric (the first
metric of the type with a `finops_cold`).

`Recommendation` is unchanged. `target_sku` is a free string per type (§8).

## 5. Evaluation rules

Per metric `m` with config `c` (see §6), series `s` (values for `c.aggregation` after derivation):

**Ops** (`c.ops_hot` set): `observed = reduce(s, c.reduce)` where `reduce ∈ {mean, max, sum}`
(default `mean`). Hot when `observed >= ops_hot` if `hot_when == above`, else `observed <= ops_hot`.
Empty series → `Skip(no_ops_data)` as today.

**FinOps** (`c.finops_cold` set): coverage as today. `p = percentile(s, P)` with `P = windows.finops.percentile`
if `hot_when == above`, else `100 - P` (peak usage of an inverted metric is its low percentile).
Cold when `p < finops_cold` (above) or `p > finops_cold` (below). The row writes `Percentile = P'`,
the percentile actually taken, so the value is read correctly.

**Finding rule for a resource in FinOps:** the primary metric must have coverage and be cold. Every
other FinOps metric that *has coverage* must be cold too, or there is no finding. A secondary metric
with insufficient coverage does not block the finding; the recommender lowers confidence and says so
in `Reason` (this is exactly the MVP's "memory not evaluated" behaviour, now data-driven).

**Applicability:** `c.applies_to: {prop: [values]}` — the metric is evaluated only when every listed
prop of the resource is in the listed values. Non-applicable metrics are neither evaluated nor
counted as skips.

**Tag overrides** work unchanged: `o11y-threshold-<metric_key>-<hot|cold>`.

## 6. Configuration

```yaml
resource_types:
  vm:
    namespace: Microsoft.Compute/virtualMachines
    granularity: {ops: PT1M, finops: PT1H}     # optional per-type override of windows.*.granularity
    metrics:
      cpu:
        metric_name: Percentage CPU
        unit: Percent
        ops_hot: 90
        finops_cold: 20
      memory:
        metric_name: Available Memory Percentage
        unit: Percent
        hot_when: below        # default above
        ops_hot: 10            # avg available memory <= 10 %
        finops_cold: 70        # P5 available memory > 70 % (peak use < 30 %)
      os_disk_iops:
        metric_name: OS Disk IOPS Consumed Percentage
        ops_hot: 90            # ops-only: no finops_cold
    recommend:                 # validated by the type's rules model
      min_vcpu: 1
  sqldb:
    namespace: Microsoft.Sql/servers/databases
    metrics:
      dtu:
        metric_name: dtu_consumption_percent
        applies_to: {purchasing_model: [dtu]}
        ops_hot: 90
        finops_cold: 20
      cpu:
        metric_name: cpu_percent
        applies_to: {purchasing_model: [vcore]}
        ops_hot: 90
        finops_cold: 20
      …
  sqlmi:
    metrics:
      storage:
        metric_name: storage_percent          # display name of the derived series
        derive: ratio_percent
        inputs: [storage_space_used_mb, reserved_storage_mb]
        ops_hot: 90
  eventhub:
    metrics:
      throttled:
        metric_name: ThrottledRequests
        unit: Count
        aggregation: Total
        reduce: sum
        ops_hot: 1
      ingress:
        metric_name: IncomingBytes
        aggregation: Total
        derive: bytes_per_second_percent      # per-interval total ÷ interval seconds ÷ capacity
        inputs: [IncomingBytes]
        capacity_prop: capacity_bytes_per_second   # from props (sku.capacity × unit rate)
        finops_cold: 30
```

`MetricThreshold` fields: `metric_name`, `unit` (default Percent), `aggregation` (default Average),
`hot_when` (default above), `reduce` (default mean), `ops_hot?`, `finops_cold?` (both optional; a
  metric with neither is fetched on FinOps runs as a recommender input only, see §7.6),
`applies_to?`, `derive?`, `inputs?`, `capacity_prop?`. A type's `recommend:` block is validated at
startup by that type's pydantic rules model (unknown keys fail, as everywhere in `config/`).

New catalogs (all "config over code"):
- `config/sql-skus.yaml`: DTU service objectives with their DTUs, pool eDTU sizes per tier, and the
  vCore size ladder.
- `config/postgres-skus.yaml`: same shape as `vm-skus.yaml` (family, vcpu, memory), reused by the
  same catalog model.
- `config/vm-skus.yaml`: unchanged.

`RESOURCE_TYPES` (env/app setting, optional): comma-separated subset of configured types to run, for
staged rollout. Default: every type present in the merged thresholds config. Unknown names fail
startup.

## 7. Type specifics

### 7.1 VM (`vm`)
- Inventory: unchanged query; props `vm_size`, `os_type`, `power_state`. `active`: power state
  running.
- Metrics: `cpu`, `memory` (Available Memory Percentage, inverted), ops-only `os_disk_iops`,
  `os_disk_bandwidth`, `vm_uncached_iops`, `vm_uncached_bandwidth` (all Percent, Average, PT1M, no
  dimensions). `Data Disk *` metrics are dimensioned by LUN and are left out (the undimensioned
  rollup averages across disks and hides a single hot disk).
- Recommend: existing ladder rule; memory clause is live: cold requires P5 available > `finops_cold`
  when memory has coverage. Confidence `medium` with both, `low` when memory had no data.
- Pricing: unchanged (VM only in this iteration).

### 7.2 SQL Database (`sqldb`)
- Inventory: `microsoft.sql/servers/databases`, excluding `master`, data warehouses
  (`kind contains 'datawarehouse'`), and non-`Online` status (auto-paused serverless DBs are `Paused`
  and are skipped with reason `not_online`). Props: `tier` (sku.tier), `sku_name`, `capacity`,
  `purchasing_model` (`dtu` for Basic/Standard/Premium, `serverless` when sku.name contains `_S_`,
  else `vcore`), `hyperscale` (bool), `pool_id` (non-empty when in an elastic pool → the DB is
  skipped from FinOps; pools are sized as a whole).
- Metrics: `dtu` (dtu, ops+finops), `cpu` (vcore, ops+finops), `app_cpu` (`app_cpu_percent`,
  serverless, ops only), `storage` (`storage_percent`, not hyperscale, ops), `workers`
  (`workers_percent`, ops).
- Recommend: DTU → next lower service objective in the same tier from `sql-skus.yaml` whose DTUs ≥
  `ceil(current_dtus × P95/100 × headroom)`; vCore → smallest ladder size ≥
  `ceil(vcores × P95/100 × headroom)`, not below `min_vcores`; same `sku_name` prefix (`GP_Gen5_`).
  `headroom` default 1.3. No pricing (cost columns null; Reason says "pricing not implemented for
  SQL").

### 7.3 SQL Elastic Pool (`sqlpool`)
- Inventory: `microsoft.sql/servers/elasticpools`; props `tier`, `capacity`, `purchasing_model`.
- Metrics: `dtu` (`dtu_consumption_percent`, dtu), `cpu` (`cpu_percent`, vcore), `storage`
  (`storage_percent`, ops).
- Recommend: eDTU → next lower pool size in tier; vCore → ladder, same as 7.2.

### 7.4 SQL Managed Instance (`sqlmi`)
- Inventory: `microsoft.sql/managedinstances`, `state == Ready`; props `tier`, `vcores`
  (sku.capacity), `storage_gb`.
- Metrics: `cpu` (`avg_cpu_percent`, ops+finops), `storage` (derived `ratio_percent` of
  `storage_space_used_mb` / `reserved_storage_mb`, ops).
- Recommend: vCore ladder (`[4, 8, 16, 24, 32, 40, 64, 80]` in `sql-skus.yaml`), not below
  `min_vcores`.

### 7.5 PostgreSQL Flexible Server (`postgres`)
- Inventory: `microsoft.dbforpostgresql/flexibleservers`, `state == Ready`; props `sku_name`,
  `tier`, `storage_gb`.
- Metrics: `cpu` (`cpu_percent`, ops+finops), `memory` (`memory_percent`, ops+finops), `storage`
  (`storage_percent`, ops), `disk_iops` (`disk_iops_consumed_percentage`, ops).
- Recommend: `postgres-skus.yaml` ladder, same family, next smaller, `min_vcpu`; both CPU and
  memory cold (memory is a normal "high is hot" metric here).

### 7.6 Cosmos DB (`cosmos`)
- Inventory: `microsoft.documentdb/databaseaccounts`; props `api_kind`, `capacity_mode`
  (`serverless` when capabilities contain `EnableServerless`, else `provisioned`).
- Granularity override: ops `PT5M`, finops `PT1H`.
- Metrics: `ru` (`NormalizedRUConsumption`, Maximum, ops+finops), `throttled`
  (`ThrottledRequestPercentage`, Average, ops), `provisioned` (`ProvisionedThroughput`, Maximum,
  finops input only: no thresholds, provisioned accounts only), `autoscale_max`
  (`AutoscaleMaxThroughput`, Maximum, input only).
- Recommend (provisioned accounts): `target = max(400, ceil(P95 × provisioned / 100 × headroom)`
  rounded up to 100 RU/s. If `autoscale_max` has data the account is autoscale: recommend lowering the
  autoscale max instead. If the RU series has high variance (P95 / median ≥ `autoscale_ratio`,
  default 3) recommend autoscale. `target_sku` is `"<n> RU/s"` or `"autoscale <n> RU/s max"`.
  Account-level `ProvisionedThroughput` is the maximum over containers, so Reason says "verify per
  container". Confidence `low`.

### 7.7 Event Hubs namespace (`eventhub`)
- Inventory: `microsoft.eventhub/namespaces`; props `tier` (sku.name), `capacity` (sku.capacity),
  `auto_inflate` (bool), `max_tu`, `capacity_bytes_per_second` = `capacity × unit_rate(tier)` with
  `unit_rate` from the type's recommend rules (`mb_per_tu: 1`, `mb_per_pu: 5`, Dedicated: none).
- Metrics: `throttled` (`ThrottledRequests`, Total, reduce sum, ops), `cpu` (`NamespaceCpuUsage`,
  Premium only, ops), `ingress` (derived `bytes_per_second_percent` of `IncomingBytes`, finops;
  Basic/Standard/Premium only).
- Recommend: `target_units = max(1, ceil(capacity × P95/100 × headroom))`; `target_sku`
  `"Standard 2 TU"`. Standard→Basic is never recommended (needs entity-level checks; Reason says so).

### 7.8 Virtual Network subnets (`vnet`)
- Inventory: `microsoft.network/virtualnetworks | mv-expand subnets`; one `Resource` per subnet with
  `id` = subnet id, props `prefix`, `ip_usable` = Σ over prefixes of `2^(32−len) − 5`, `ip_used` =
  `array_length(ipConfigurations)`, `utilization_percent`. Delegated subnets and subnets with
  `ip_usable <= 0` are kept but produce no finding when `ip_used` is 0. `sku` is the prefix list.
- `metric_source: inventory`: `subnet_ip` (`SubnetIpUtilization`, Percent, ops) reads
  `props["utilization_percent"]`. No Monitor call, no FinOps.
- Known gap (documented in `gotchas.md`): VMSS-uniform NICs are not ARM resources and are absent
  from `ipConfigurations`, so those subnets read low.

## 8. Findings rows

No schema change. Column conventions per type:

| Column | vm | sqldb / sqlpool | sqlmi | postgres | cosmos | eventhub | vnet |
|---|---|---|---|---|---|---|---|
| `Sku` | `Standard_D4s_v5` | `GP_Gen5_4`, `S3`, `StandardPool 100` | `GP_Gen5 8 vCores` | `Standard_D4ds_v5` | `provisioned` / `serverless` | `Standard 4 TU` | `10.0.1.0/24` |
| `OsType` | Linux/Windows | "" | "" | "" | "" | "" | "" |
| `RecommendedSku` | SKU name | service objective / pool size / `GP_Gen5_2` | `GP_Gen5 4 vCores` | SKU name | `1200 RU/s` | `Standard 2 TU` | n/a |
| `MetricNamespace` | Monitor namespace | ← | ← | ← | ← | ← | `Microsoft.Network/virtualNetworks` |
| `Aggregation` | as configured | ← | `Average (derived)` | ← | ← | `Total (derived)` | `computed` |

`ResourceType` is the ARM type in lower case; downstream alert rules filter on it.

## 9. Errors, skips, logging

New skip reasons: `not_online` (sqldb), `not_ready` (sqlmi, postgres), `in_elastic_pool` (sqldb,
FinOps only), `no_capacity_model` (eventhub Dedicated in FinOps, cosmos serverless in FinOps),
`unsupported_sku` (any recommender that cannot map the SKU). Every skip is logged with the resource
id and counted per type. A chunk failure still skips only its chunk. A type whose inventory query
fails (non-403) is logged, counted (`type_failures`), and the run continues with the other types;
403 still raises `PermissionMissing("inventory")`.

> Implementation note 2026-09-22: `unsupported_sku` was not built. A resource whose SKU a recommender
> cannot map still gets a FinOps row, with `RecommendedSku` empty, `Confidence` `low`, and a `Reason`
> that explains it — the cold finding is real, only the target is unknown. See
> [docs/agents/recommendations.md](../../agents/recommendations.md#contract).

## 10. Testing

- Unit: `evaluate/metric.py` (direction, reduce, percentile flip, applies_to, coverage),
  `metrics/derive.py`, each recommender with fixtures (cold, not-cold-secondary, no-data-secondary,
  smallest-already, unknown SKU), `metrics/batch.py` parsing multi-metric/multi-aggregation payloads.
- Fixtures: one Resource Graph page and one batch payload per type under `tests/fixtures/<type>/`.
- Pipeline: extend `FakeInventory`/`FakeMetrics` to dispatch by kind; one end-to-end dry run per mode
  asserting rows per type and skip counts; `RESOURCE_TYPES` subset; a failing inventory for one type
  does not stop the others.
- Config: every metric in `default.yaml` validates; `applies_to` props exist in the type's parser
  output (a test walks the fixture rows).

## 11. Order of work (one commit / PR each)

0. Core generalization, no behaviour change: registry, `Resource`, evaluators, multi-metric batch,
   per-type config, pipeline loop. VM CPU only. All existing tests migrated.
1. VM platform metrics (memory + disk %), memory clause in the recommender.
2. SQL Database.
3. SQL Elastic Pool.
4. SQL Managed Instance.
5. PostgreSQL Flexible Server.
6. Cosmos DB.
7. Event Hubs.
8. VNET subnets.
9. Docs: `architecture.md`, `thresholds.md`, `recommendations.md`, `findings.md`, `roadmap.md`,
   `gotchas.md`, `CLAUDE.md` ("MVP is VM CPU only" line), README.

Steps 2–8 are independent once step 0 is merged and can run in parallel worktrees.

## 12. Assumptions made without asking (autonomous session)

- **Direction over transform.** Inverted metrics are configured with `hot_when: below` and the row
  reports the metric in Azure's own terms (available %, P5) rather than a synthesized "used %". A
  reader sees the real Azure metric name and value.
- **FinOps needs every covered metric cold.** A VM with cold CPU but busy memory produces no row.
  The MVP would have produced one; this is the rule `recommendations.md` already states.
- **Pricing stays VM-only.** SQL, PostgreSQL, Cosmos and Event Hubs rows carry null cost columns and
  a Reason that says so. Retail Prices filters for those services need live verification; tracked in
  the roadmap.
- **Cosmos is evaluated at account level.** Container-level dimension splitting is a follow-up.
- **Databases in elastic pools are Ops-only.** The pool gets the FinOps row.
- **One row per (resource, metric) in Ops** stays; no cross-metric Ops correlation.
- **`RESOURCE_TYPES` defaults to all configured types.** Operators narrow it per environment during
  rollout; nothing else changes between styles.
- **Guest-OS metrics remain out.** The `law` identity requirement stays reserved but unused.
