# Roadmap and adding a resource type

Read this before starting a new resource type or picking up a backlog item.

## Done

The MVP (VM `Percentage CPU`) and the seven types of the architecture table: VM platform metrics
(memory, disk %), Azure SQL Database, SQL Elastic Pool, SQL Managed Instance, PostgreSQL Flexible Server,
Cosmos DB, Event Hubs, VNET subnets (spec: `docs/superpowers/specs/2026-09-22-resource-types-design.md`).

## Done (second round)

Items 1–6 of the previous list (spec: `docs/superpowers/specs/2026-09-22-next-resource-types-design.md`):
App Service Plans (`appserviceplan`), AKS clusters (`aks`), Azure Cache for Redis (`redis`), Service Bus
namespaces (`servicebus`), Application Gateway (`appgateway`), Storage accounts (`storage`). They added
three config-driven generalizations to `metrics/` (`dimension` filters, `percent_of_capacity`,
`missing_as_zero`; see [thresholds](thresholds.md#metric-fields)) and no new permission.

## Next resource types (suggested order)

Each is platform metrics or Resource Graph only, so it needs no new permission and no agent:

1. **Orphaned resources** (Resource Graph only, zero metrics, pure FinOps): unattached managed disks,
   unassociated public IPs and NICs, empty load balancers and app gateways, stopped-but-allocated VMs.
   Cheapest wins in an enterprise estate.
2. **VM availability** (`VmAvailabilityMetric`) as an SLA signal on the existing VM type.
3. **Azure Cache for Redis Enterprise** (`Microsoft.Cache/redisEnterprise`): a separate ARM type with its
   own metrics; the `redis` type covers Basic/Standard/Premium only.

## Adding a resource type

One PR per type. The pipeline does not change; a type is:

1. `src/resource_types/<type>.py`: `KIND`, `ARM_TYPE`, `QUERY` (Resource Graph KQL), `parse` (row →
   `Resource` via `registry.base_resource`, type facts in `props`), `active` (skip before metrics;
   `registry.require_state` for the common "is it running" check), optional `finops_skip`, optional
   `enrich` (facts that need the type's rules), and `SPEC` (`ResourceTypeSpec`, including
   `catalog=CatalogSource(...)` when the recommender reads a SKU catalog), registered with one line in
   `src/resource_types/__init__.py`.
2. A block under `resource_types:` in `config/thresholds/default.yaml`: namespace, optional
   `granularity` override, one entry per metric key ([thresholds](thresholds.md#metric-fields)), and the
   recommender knobs.
3. `src/recommend/<type>.py`: the rules model (pydantic, `extra="forbid"`) and the pure recommender
   ([recommendations](recommendations.md)); catalogs go in `config/*.yaml` and are loaded through
   `SPEC.catalog`, so `config/models.py` and the loader stay untouched.
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
- **AKS per-node-pool evaluation** with the batch `filter` on `nodepool`. Needs one metric response
  split into several evaluated units, which the pipeline cannot do; today the cluster rollup drives a
  per-pool recommendation marked "verify per node pool".
- **App Gateway `ResponseStatus` 5xx** (`HttpStatusGroup` filter) as a share of all responses: the
  filtered and unfiltered series of one raw metric cannot share a run mode today; `FailedRequests` /
  `TotalRequests` is the Ops proxy.
- **Storage tiering by capacity growth** (`UsedCapacity` slope) and per-container access patterns; today
  the account-level transaction rate is the signal.
- **VM guest metrics via LAW** (`law` identity requirement, reserved): only if a signal is missing from
  platform metrics; memory is covered by `Available Memory Percentage` now.
- **Subnet utilization for VMSS-uniform NICs** (not in Resource Graph; see `docs/gotchas.md`).
