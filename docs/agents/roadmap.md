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
6. Report section

If the type needs a new permission, follow `identity.md` before writing code.

## Backlog: write an ADR in `docs/adr/` before implementing

- **Report hosting for outside teams.** Compare (a) a static website on the storage account, with
  Markdown rendered to HTML at publish time behind a private endpoint, (b) a Teams channel file upload via
  Graph with a link in the alert, and (c) GitLab Pages or wiki. Pick the option that needs the fewest new
  permissions.
- **Email transport.** Confirm the bank's SMTP relay; otherwise use a Logic App or ACS Email.
- **Datadog.** Forward only the Ops and FinOps alerts that pass a per-MG "send to Datadog" filter. Compare
  the Events API v2 and Logs intake (API key in Key Vault) against a webhook integration. The existing
  Datadog Azure integration already pulls Azure Monitor metrics, so agree the boundary with Enterprise
  Observability before building.
- **Per-subscription deployment mode**, the fallback if MG-scope performance or permissions fail.
- **Future types:** AKS, App Service Plans, Storage, Redis, Function Apps.
