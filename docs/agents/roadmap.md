# Roadmap and adding a resource type

Read this before starting a new resource type or picking up a backlog item.

## Order of work

The MVP (VM `Percentage CPU`, end to end) is done. Next comes one resource type per iteration and per PR:

VM memory/disk (LAW) → Cosmos DB → SQL DB → SQL MI → PostgreSQL → Event Hubs → VNET.

## Adding a resource type

Work in this order, one PR per type:

1. Resource Graph inventory query (`src/inventory/`)
2. Metric definitions (`src/metrics/`)
3. Threshold defaults (`config/thresholds/default.yaml`)
4. Recommend module (`src/recommend/<type>.py`, per `recommendations.md`)
5. Tests with recorded fixtures
6. Findings rows: reuse the shared columns; add type-specific columns at the end of
   `schema/findings-tables.json` only if needed (`findings.md`)

If the type needs a new permission, follow `identity.md` before writing code.

## Backlog: write an ADR in `docs/adr/` before implementing

- **Downstream alerting on the findings tables.** Log search alert rules and action groups (Ops channel,
  owner / assignment-group email) and a FinOps workbook. Decide whether they live here or with
  Enterprise Observability.
- **Datadog.** Forward findings that pass a per-MG "send to Datadog" filter, ideally from the LAW tables
  rather than from the function. The existing Datadog Azure integration already pulls Azure Monitor
  metrics, so agree the boundary with Enterprise Observability before building.
- **Per-subscription deployment mode**, the fallback if MG-scope performance or permissions fail.
- **Future types:** AKS, App Service Plans, Storage, Redis, Function Apps.
