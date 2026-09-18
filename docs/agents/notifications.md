# Notifications and reports

Read this before changing alert routing, a notifier (`src/notify/`), or the FinOps report.

## Ownership tags

Tag names are configurable (`tags:` in the thresholds config). These are the defaults:

| Tag | Meaning | Used for |
|-----|---------|----------|
| `owner` | Owner's email address | Direct email. A malformed address counts as missing |
| `assignment_group` | Owning team's name | Looked up in `config/assignment-groups.yaml` → team email and optional Teams webhook |
| `car_id` | Numeric application ID | Shown in every alert and report row, and used as the report's grouping key. Never routes on its own |

`config/assignment-groups.yaml` (`<assignment_group>: {email, teams_webhook}`) is maintained by hand and
validated at startup. Unknown keys and malformed emails fail loudly.

## Routing

For each alert, in order:

1. The Ops channel, always.
2. The `owner` email, if present.
3. The `assignment_group` email and webhook, if mapped.

When a value is missing or unmapped, the alert goes to the Ops channel only. The alert body and the
report footer then name the exact tag that was missing or unmapped, so tag hygiene becomes visible.
Route only to owners the tags name; never guess an owner.

## Transports

- **Teams:** a Power Automate "Workflows" incoming webhook with an Adaptive Card payload. Office 365
  connectors are retired, so don't use them. Webhook URLs live in Key Vault. `config/routing.yaml` holds
  the Ops webhook secret name and per-MG overrides.
- **Email:** an SMTP relay (the bank's relay is still to be confirmed). Keep the sender behind the
  `Notifier` interface, so that moving to Logic App or ACS is a one-file change.

## FinOps report

- Markdown, one file per resource type per day:
  `reports/<yyyy-mm-dd>/<mg-id>/<resource-type>.md` in the `reports` container. Later runs that day
  overwrite it.
- Row columns: resource, subscription, car_id, assignment_group, owner, current SKU, observed P95
  utilization, recommended SKU/units, current monthly cost, projected monthly cost, estimated saving.
- Footer: totals, the excluded-resources appendix, the ignored-RG count, and missing or unmapped tags.
- Prices come from the Azure Retail Prices API (`https://prices.azure.com/api/retail/prices`), which is
  unauthenticated. Cache prices per run.
