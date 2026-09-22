# Roadmap and adding a resource type

Read this before starting a new resource type or picking up a backlog item.

## Done

The MVP (VM `Percentage CPU`) and the seven types of the architecture table: VM platform metrics
(memory, disk %), Azure SQL Database, SQL Elastic Pool, SQL Managed Instance, PostgreSQL Flexible Server,
Cosmos DB, Event Hubs, VNET subnets (spec: `docs/superpowers/specs/2026-09-22-resource-types-design.md`).

## Next resource types (suggested order)

Each is platform metrics or Resource Graph only, so it needs no new permission and no agent:

1. **App Service Plans** (`Microsoft.Web/serverfarms`): `CpuPercentage`, `MemoryPercentage` hot;
   cold → smaller tier / fewer instances. Common, expensive, rarely right-sized.
2. **AKS node pools** (`Microsoft.ContainerService/managedClusters`):
   `node_cpu_usage_percentage`, `node_memory_working_set_percentage`, `kube_pod_status_ready` hot;
   cold → smaller node SKU or fewer nodes (per agent pool via the `node` dimension).
3. **Azure Cache for Redis** (`Microsoft.Cache/redis`): `percentProcessorTime`, `usedmemorypercentage`,
   `serverLoad`, `errors` hot; cold → smaller cache size / tier.
4. **Service Bus namespaces** (`Microsoft.ServiceBus/namespaces`): `ThrottledRequests`,
   `DeadletteredMessages`, `ServerErrors` hot (SLA); Premium `NamespaceCpuUsage`, cold → fewer messaging
   units. Same shape as Event Hubs.
5. **Application Gateway** (`Microsoft.Network/applicationGateways`): `UnhealthyHostCount`,
   `FailedRequests`, `ResponseStatus` 5xx hot; v2 `CapacityUnits` / `ComputeUnits` P95 vs `minCapacity`
   cold → lower minimum instance count.
6. **Storage accounts** (`Microsoft.Storage/storageAccounts`): `Availability`, throttling
   (`Transactions` split by `ResponseType` = `ServerBusyError`/`ThrottlingError`) hot; cost → lifecycle
   tiering candidates from `UsedCapacity` growth. Needs the batch `filter` parameter for dimensions.
7. **Orphaned resources** (Resource Graph only, zero metrics, pure FinOps): unattached managed disks,
   unassociated public IPs and NICs, empty load balancers and app gateways, stopped-but-allocated VMs.
   Cheapest wins in an enterprise estate.
8. **VM availability** (`VmAvailabilityMetric`) as an SLA signal on the existing VM type.

## Adding a resource type

One PR per type. The pipeline does not change; a type is:

1. `src/resource_types/<type>.py`: `KIND`, `ARM_TYPE`, `QUERY` (Resource Graph KQL), `parse` (row →
   `Resource`, type facts in `props`), `active` (skip before metrics), optional `finops_skip`, and `SPEC`
   (`ResourceTypeSpec`), registered with one line in `src/resource_types/__init__.py`.
2. A block under `resource_types:` in `config/thresholds/default.yaml`: namespace, optional
   `granularity` override, one entry per metric key ([thresholds](thresholds.md#metric-fields)), and the
   recommender knobs.
3. `src/recommend/<type>.py`: the rules model (pydantic, `extra="forbid"`) and the pure recommender
   ([recommendations](recommendations.md)); catalogs go in `config/*.yaml`.
4. Fixtures under `tests/fixtures/<type>/` (one Resource Graph page, one batch payload),
   `tests/test_resource_types_<type>.py` (parser, active/skip, end-to-end dry run through
   `pipeline.run`), `tests/test_recommend_<type>.py`.
5. Docs: the table in `findings.md`, the rule in `recommendations.md`, this file.
6. Findings rows reuse the shared columns; add type-specific columns at the end of
   `schema/findings-tables.json` only if needed (`findings.md`).

If the type needs a new permission (everything so far is Reader + Monitoring Reader on the MG), follow
`identity.md` before writing code. If a metric surprises you (minimum grain, dimensions, missing
`resourceid`), pin it in `docs/gotchas.md`.

## Backlog: write an ADR in `docs/adr/` before implementing

- **Downstream alerting on the findings tables.** Log search alert rules and action groups (Ops channel,
  owner / assignment-group email) and a FinOps workbook. Decide whether they live here or with
  Enterprise Observability.
- **Datadog.** Forward findings that pass a per-MG "send to Datadog" filter, ideally from the LAW tables
  rather than from the function. The existing Datadog Azure integration already pulls Azure Monitor
  metrics, so agree the boundary with Enterprise Observability before building.
- **Per-subscription deployment mode**, the fallback if MG-scope performance or permissions fail.
- **Pricing for SQL, PostgreSQL, Cosmos DB, and Event Hubs.** Retail Prices filters differ per service
  (`serviceName`, `skuName`, `meterName`) and need live verification; until then those rows carry null
  cost columns.
- **Cosmos DB per-container evaluation** with the batch `filter` on `CollectionName`; today the
  account-level maximum drives the recommendation.
- **VM guest metrics via LAW** (`law` identity requirement, reserved): only if a signal is missing from
  platform metrics; memory is covered by `Available Memory Percentage` now.
- **Subnet utilization for VMSS-uniform NICs** (not in Resource Graph; see `docs/gotchas.md`).
