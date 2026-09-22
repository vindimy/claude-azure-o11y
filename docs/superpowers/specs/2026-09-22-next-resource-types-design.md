# Design: the next six resource types

Date: 2026-09-22
Status: approved for implementation (autonomous session; assumptions listed in §6)
Scope: items 1–6 of "Next resource types" in `docs/agents/roadmap.md`: App Service Plans, AKS
clusters, Azure Cache for Redis, Service Bus namespaces, Application Gateway, Storage accounts.
Platform metrics and Resource Graph only; no new permission (Reader + Monitoring Reader on the MG
cover every call below).

## 1. Goal

| Type | Config key | Ops (hot) | FinOps (cold → recommendation) |
|---|---|---|---|
| App Service Plan | `appserviceplan` | CPU %, memory %, HTTP queue length | CPU + memory → fewer instances, else smaller SKU in the family; an empty plan → delete |
| AKS cluster | `aks` | node CPU %, node working-set memory %, node disk %, unschedulable pods | node CPU + memory → fewer nodes (or a lower autoscaler minimum) per pool, else a smaller node SKU |
| Azure Cache for Redis | `redis` | CPU %, used memory %, server load %, errors | CPU + memory → next smaller cache size in the same family |
| Service Bus namespace | `servicebus` | throttled requests, server errors, dead-lettered messages, Premium CPU/memory | Premium CPU + memory → fewer messaging units |
| Application Gateway | `appgateway` | unhealthy hosts, failed-request %, v1 CPU | v2 capacity units vs reserved (instances × 10 CU) → lower minimum / fixed instance count |
| Storage account | `storage` | availability (low is hot), throttled transactions | Hot-tier blob accounts with few transactions → Cool access tier / lifecycle rule |

Success: every type produces rows from recorded fixtures in dry run, `make test` stays ≥ 80 % on
`evaluate/` and `recommend/`, `make lint` passes, `schema/findings-tables.json` is untouched, and
`identity/role-requirements.yaml` is untouched.

## 2. What the pipeline cannot express today

Three small, config-driven generalizations, all pure and all in `metrics/` + `config/models.py`.
The pipeline loop, the evaluators, and the row builders do not change.

1. **Dimension filters.** Storage throttling is `Transactions` split by `ResponseType`. The batch API
   takes one `filter` per call and applies it to every metric in the call, so a filtered metric must
   travel in its own call. `MetricThreshold.dimension: {name, values}` renders the OData filter
   (`ResponseType eq 'A' or ResponseType eq 'B'`) and `rollupby=<name>` so Azure returns one
   series; `MetricRequest` carries `filter` and `roll_up_by`; `MetricsBatchClient.query` groups
   requests by `(filter, roll_up_by)` and issues one `query_resources` call per group, merging the
   series. If the response still holds several timeseries for a filtered request, they are summed
   per timestamp (the filter is for count metrics). The startup validator that rejects one raw
   metric with two aggregations in one mode also rejects two different filters.
2. **`percent_of_capacity` derivation.** App Gateway's FinOps signal is `CapacityUnits` (Average)
   as a share of the reserved units, a resource prop. `derive: percent_of_capacity` with
   `inputs: [<metric>]` and `capacity_prop` computes `value / capacity × 100` per point; the metric
   is dropped when the prop is missing, like `bytes_per_second_percent`.
3. **`missing_as_zero`.** Azure returns a timestamp with no value for an interval in which a count
   metric had nothing to count. For Storage `Transactions` that is the interesting case (an idle
   account), so `missing_as_zero: true` turns those points into `0` before evaluation. Points Azure
   did not return at all stay absent (coverage still guards a metric that does not exist for the
   resource).

## 3. Type specifics

Shared conventions: KQL projects `id, name, subscriptionId, resourceGroup, location, tags` plus the
type's columns; `parse` is pure; `active` uses `registry.require_state`; every skip reason is
listed in `findings.md`; recommenders never mention pricing (`SPEC.priced=False` for all six, the
pipeline appends `UNPRICED_NOTE`).

### 3.1 App Service Plan (`appserviceplan`)

- ARM `microsoft.web/serverfarms`, namespace `Microsoft.Web/serverfarms`.
- KQL excludes consumption plans (`sku.tier` in `Dynamic`, `FlexConsumption`: billed per execution,
  nothing to size). Columns: `kind`, `sku.name`, `sku.tier`, `sku.capacity`, `properties.status`,
  `properties.numberOfSites`, `properties.elasticScaleEnabled`, `properties.reserved`.
- Props: `sku_name`, `tier`, `instances`, `status`, `sites`, `elastic_scale`, `os`
  (`linux`/`windows` from `reserved`), `plan_kind`. `Sku` = `P1v3 x3`.
- `active`: `status == Ready` (`not_ready`). `finops_skip`: Free/Shared tiers → `no_capacity_model`.
- Metrics: `cpu` (`CpuPercentage`, hot 90, cold 20), `memory` (`MemoryPercentage`, hot 90,
  cold 40), `http_queue` (`HttpQueueLength`, Count, hot 100). The `Instance` dimension is not
  filtered: the rollup is the average across instances.
- Catalog `config/appservice-skus.yaml` (`FamilySkuCatalog`): B1–B3, S1–S3, P1v2–P3v2,
  P0v3–P3v3, P1mv3–P5mv3, I1v2–I6v2, EP1–EP3, WS1–WS3.
- Recommend (`recommend/appserviceplan.py`, knobs `min_instances` 1, `min_vcpu` 1, `headroom`
  1.3): peak = max(P95 CPU, P95 memory); confidence `medium` with both, `low` without memory.
  `sites == 0` → target `delete`. Else if not elastic and `instances > min_instances`:
  `n = max(min_instances, ceil(instances × peak/100 × headroom))`; `n < instances` → `<sku> x<n>`.
  Else next smaller SKU in the family (`ladder.next_smaller_in_family`) when
  `peak × headroom × current.vcpu / target.vcpu ≤ 100` → `<smaller> x<instances>`. Unknown SKU →
  no target, `low`. Otherwise no target with the reason.

### 3.2 AKS cluster (`aks`)

- ARM `microsoft.containerservice/managedclusters`, namespace
  `Microsoft.ContainerService/managedClusters`. Columns: `sku.tier`, `properties.powerState.code`,
  `properties.kubernetesVersion`, `properties.agentPoolProfiles` (array).
- Props: `tier`, `power_state`, `k8s_version`, `pools` (list of `{name, vm_size, count, mode,
  autoscale, min_count, max_count}`), `node_count`. `Sku` = `Standard_D4s_v5 x3 + Standard_D8s_v5 x2`
  (pools in profile order).
- `active`: `power_state == Running` (`not_running`).
- Metrics (cluster-level rollups; the `node`/`nodepool` dimensions are not filtered): `node_cpu`
  (`node_cpu_usage_percentage`, hot 90, cold 20), `node_memory`
  (`node_memory_working_set_percentage`, hot 90, cold 30), `node_disk`
  (`node_disk_usage_percentage`, hot 90), `unschedulable_pods`
  (`cluster_autoscaler_unschedulable_pods_count`, Count, Average, hot 1); input
  `unneeded_nodes` (`cluster_autoscaler_unneeded_nodes_count`, Count, Average). The roadmap's
  `kube_pod_status_ready` is not used: its rollup mixes the `condition` dimension values.
- Catalog: `vm-skus.yaml` (`FamilySkuCatalog`), shared with VMs (one parse).
- Recommend (`recommend/aks.py`, knobs `min_nodes` 1, `min_vcpu` 2, `headroom` 1.3): peak =
  max(P95 CPU, P95 memory). Per pool: `current` = `min_count` for autoscaling pools, else `count`;
  `n = max(min_nodes, ceil(current × peak/100 × headroom))`; `n < current` → `<pool>: <vm_size> x<n>`
  (autoscale: `<pool>: min <n>`). A pool already at `min_nodes` → next smaller node SKU in the
  family when it fits (`<pool>: <smaller> x<current>`). `target_sku` joins the pool changes with
  `; `; none → no target. Confidence always `low`; the reason ends with "Cluster-level metric;
  verify per node pool." and mentions the autoscaler's unneeded-node count when present.

### 3.3 Azure Cache for Redis (`redis`)

- ARM `microsoft.cache/redis`, namespace `Microsoft.Cache/redis`. Enterprise tiers
  (`microsoft.cache/redisenterprise`) are a different type and out of scope. Columns:
  `properties.sku.name`, `.family`, `.capacity`, `properties.provisioningState`,
  `properties.shardCount`, `properties.redisVersion`.
- Props: `tier`, `family`, `capacity`, `size` (`C1`), `state`, `shards`. `Sku` = `Standard C1`.
- `active`: `state == Succeeded` (`not_ready`).
- Metrics: `cpu` (`percentProcessorTime`, hot 90, cold 20), `memory` (`usedmemorypercentage`,
  hot 90, cold 30), `server_load` (`serverLoad`, hot 90), `errors` (`errors`, Count, Maximum,
  reduce max, hot 1). Shard rollups are used as-is.
- Catalog `config/redis-skus.yaml`, model `RedisSkuCatalog` in `recommend/redis.py`
  (`{family, memory_gb}` per size: C0 0.25 … C6 53, P1 6 … P5 120).
- Recommend (`recommend/redis.py`, knobs `min_capacity` 0, `headroom` 1.3): next smaller size in
  the same family with `capacity ≥ min_capacity` and, when memory has data,
  `current_gb × P95 memory/100 × headroom ≤ target_gb`. `Standard C1` → `Standard C0`. Confidence
  `medium` with memory, `low` without. Premium→Standard is never recommended (clustering,
  persistence, VNet); the reason says so.

### 3.4 Service Bus namespace (`servicebus`)

- ARM `microsoft.servicebus/namespaces`, namespace `Microsoft.ServiceBus/namespaces`. Columns:
  `sku.name`, `sku.capacity`, `properties.premiumMessagingPartitions`, `properties.status`.
- Props: `tier`, `capacity`, `partitions`, `status`. `Sku` = `Premium 2 MU`, `Standard`, `Basic`.
- `active`: `status == Active` (`not_active`). `finops_skip`: not Premium → `no_capacity_model`.
- Metrics: `throttled` (`ThrottledRequests`, Count, Total, sum, hot 1), `server_errors`
  (`ServerErrors`, Count, Total, sum, hot 1), `deadlettered` (`DeadletteredMessages`, Count,
  Maximum, reduce max, hot 100: the deepest dead-letter queue in the window), `cpu`
  (`NamespaceCpuUsage`, Premium, hot 90, cold 20), `memory` (`NamespaceMemoryUsage`, Premium,
  hot 90, cold 30).
- Recommend (`recommend/servicebus.py`, knobs `headroom` 1.3, `min_units` 1, `messaging_units`
  `[1, 2, 4, 8, 16]`): peak = max(P95 CPU, P95 memory); `ladder.fit_down` over the unit ladder →
  `Premium 1 MU`. Confidence `medium` with both metrics, `low` without memory. Premium→Standard is
  never recommended; partitioned namespaces are named in the reason.

### 3.5 Application Gateway (`appgateway`)

- ARM `microsoft.network/applicationgateways`, namespace `Microsoft.Network/applicationGateways`.
  The SKU lives under `properties.sku`. Columns: `properties.sku.name`, `.tier`, `.capacity`,
  `isnotnull(properties.autoscaleConfiguration)`, `.minCapacity`, `.maxCapacity`,
  `properties.operationalState`.
- Props: `sku_name`, `tier`, `v2` (tier ends with `_v2`), `autoscale`, `capacity`, `min_capacity`,
  `max_capacity`, `state`; `enrich` adds `reserved_capacity_units` =
  `(min_capacity if autoscale else capacity) × cu_per_instance` for v2 when positive. `Sku` =
  `Standard_v2 autoscale 2-10`, `WAF_v2 x3`, `Standard_Medium x2`.
- `active`: `state == Running` (`not_running`). `finops_skip`: v1, or a v2 with no reserved units
  (autoscale minimum 0) → `no_capacity_model`.
- Metrics: `unhealthy_hosts` (`UnhealthyHostCount`, Count, Average, hot 1), `failed_requests`
  (display `FailedRequestPercentage`, `derive: ratio_percent`, inputs `FailedRequests` /
  `TotalRequests`, Total, hot 5), `cpu` (`CpuUtilization`, v1 only, hot 90), `capacity` (display
  `CapacityUnitsPercentage`, `derive: percent_of_capacity`, input `CapacityUnits` Average,
  `capacity_prop: reserved_capacity_units`, v2 only, cold 30). `ResponseStatus` 5xx needs a
  dimension filter on `HttpStatusGroup` and would collide with an unfiltered `ResponseStatus` in
  the same mode; `FailedRequests` is the 5xx proxy.
- Recommend (`recommend/appgateway.py`, knobs `cu_per_instance` 10, `min_instances` 1, `headroom`
  1.3): `current` = `min_capacity` (autoscale) or `capacity`;
  `n = max(min_instances, ceil(reserved × P95/100 × headroom / cu_per_instance))`; `n < current` →
  `<sku_name> autoscale <n>-<max>` or `<sku_name> x<n>`, confidence `medium`; else no target.

### 3.6 Storage account (`storage`)

- ARM `microsoft.storage/storageaccounts`, namespace `Microsoft.Storage/storageAccounts`
  (account-level metrics). Columns: `kind`, `sku.name`, `sku.tier`, `properties.accessTier`,
  `properties.provisioningState`, `properties.isHnsEnabled`.
- Props: `sku_name`, `tier`, `account_kind`, `access_tier`, `state`, `hns`, `tierable`
  (Standard tier, kind `StorageV2` or `BlobStorage`, access tier `Hot`). `Sku` = `Standard_LRS Hot`
  (`Premium_LRS` when there is no access tier).
- `active`: `state == Succeeded` (`not_ready`). `finops_skip`: not tierable → `not_tierable`
  (detail names the tier or kind).
- Metrics: `availability` (`Availability`, Average, `hot_when: below`, hot 99.9), `throttled`
  (`Transactions`, Count, Total, sum, `dimension: {name: ResponseType, values: [ServerBusyError,
  ClientThrottlingError]}`, `missing_as_zero`, hot 1), `transactions` (`Transactions`, Count,
  Total, `missing_as_zero`, cold 1000 per hour at P95), input `used_capacity` (`UsedCapacity`,
  Bytes, Average; PT1H grain, FinOps only).
- Recommend (`recommend/storage.py`, knobs `min_gib` 100, `target_tier` `Cool`): used GiB from
  `inputs["used_capacity"]`; missing → no target, `low`; below `min_gib` → no target; else
  `<sku_name> <target_tier>`, confidence `low`, reason "set the default access tier to Cool or add
  a lifecycle rule; account-level, verify per container and check early-deletion and retrieval
  charges." The roadmap's "UsedCapacity growth" is replaced by access density (transactions per
  hour) because the evaluators score a percentile, not a slope; capacity is the sizing input.

## 4. Findings rows

No schema change. `ResourceType` is the ARM type in lower case.

| Type | `Sku` | `RecommendedSku` | `MetricKey` values |
|---|---|---|---|
| appserviceplan | `P1v3 x3` | `P1v3 x2`, `P0v3 x3`, `delete` | `cpu`, `memory`, `http_queue` |
| aks | `Standard_D4s_v5 x3 + Standard_D8s_v5 x2` | `userpool: Standard_D8s_v5 x1; system: min 1` | `node_cpu`, `node_memory`, `node_disk`, `unschedulable_pods` |
| redis | `Standard C1` | `Standard C0` | `cpu`, `memory`, `server_load`, `errors` |
| servicebus | `Premium 2 MU`, `Standard` | `Premium 1 MU` | `throttled`, `server_errors`, `deadlettered`, `cpu`, `memory` |
| appgateway | `Standard_v2 autoscale 2-10`, `WAF_v2 x3` | `Standard_v2 autoscale 1-10`, `WAF_v2 x2` | `unhealthy_hosts`, `failed_requests`, `cpu`, `capacity` |
| storage | `Standard_LRS Hot` | `Standard_LRS Cool` | `availability`, `throttled`, `transactions` |

New skip reasons: `not_ready` (appserviceplan, redis, storage), `not_running` (aks, appgateway),
`not_active` (servicebus), `no_capacity_model` (appserviceplan Free/Shared, servicebus
Basic/Standard, appgateway v1 and autoscale-min-0, FinOps), `not_tierable` (storage, FinOps).

## 5. Testing

Per type, as for the existing eight: `tests/fixtures/<type>/resource_graph.json` (one page, one
row per interesting shape) and `metrics_batch.json` (recorded shape naming the metrics exactly as
config does); `tests/test_resource_types_<type>.py` (parser, active/skip, enrich where present,
one Ops and one FinOps dry run through `pipeline.run` with an own `FakeInventory`, reusing
`FakeMetrics`/`FakePricing`/`rows` from `tests/test_pipeline.py`, plus a `parse_batch_response`
check over the recorded payload); `tests/test_recommend_<type>.py` (every branch of the rule).
Core: `tests/test_derive.py` (percent_of_capacity, missing_as_zero), `tests/test_metrics_batch.py`
(filtered request summed per timestamp), `tests/test_config.py` (two filters on one raw metric in
one mode fail startup, dimension renders the filter string).

## 6. Assumptions made without asking (autonomous session)

- **Cluster-level AKS.** Per-pool evaluation needs the batch `filter` on `nodepool` plus splitting
  one response into several resources, which the pipeline cannot do; like Cosmos per-container, it
  is a backlog item. The recommendation applies the cluster peak to each pool and says so.
- **No `kube_pod_status_ready`.** Replaced by `cluster_autoscaler_unschedulable_pods_count`, an
  unambiguous "no capacity" signal.
- **Storage FinOps is access density, not growth.** See §3.6.
- **Storage throttling `ResponseType` values** are `ServerBusyError` and `ClientThrottlingError`
  from the Storage metrics reference; they are config, not code, so a live check can extend them.
- **App Gateway `ResponseStatus` 5xx** is deferred (§3.5); `FailedRequests` / `TotalRequests` is
  the Ops signal.
- **Dead-letter threshold** is a queue depth (100), not a rate; the Ops table shows how long it
  stays there, as for every other hot metric.
- **One commit per type** on `feat/next-resource-types`, so the branch can be split into one PR per
  type as `CLAUDE.md` requires.
- **Pricing stays VM-only.** All six write null cost columns with `UNPRICED_NOTE`.
