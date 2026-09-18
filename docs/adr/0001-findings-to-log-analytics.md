# ADR-0001: Write findings to Log Analytics tables instead of Teams and blob reports

- Status: accepted
- Date: 2026-09-17

## Context

The MVP posted Ops alerts to a Teams (Power Automate) webhook and wrote the FinOps report as a daily
Markdown blob in the `reports` container. That approach had four problems:

- Teams cards cannot be queried, aggregated, or joined with other telemetry.
- Report hosting for outside teams was still an open backlog item.
- Email and Datadog forwarding each needed a separate transport.
- Routing rules were code in this repo.

## Decision

Every finding becomes one row in a Log Analytics custom table: `O11yOpsFindings_CL` for Ops and
`O11yFinOpsFindings_CL` for FinOps. Rows go through the Logs Ingestion API (a DCE plus one DCR with two
streams). Both deployment paths create the tables, DCE, and DCR from a single schema file,
`schema/findings-tables.json`. The Teams notifier, Markdown report, `reports` container, `config/routing.yaml`,
and the `ops_webhook_secret_name` parameter are removed.

Notification and routing move downstream: log alert rules, action groups, and workbooks query the tables.

## Consequences

- New permission: `Monitoring Metrics Publisher` on the findings DCR (`findings_ingest`). The DCR is
  created by the deployment, so on a fresh environment the grant is a second IAM pass after the first
  deploy (see `terraform/examples/iam-uami`).
- `law_resource_id` becomes required. The deploying principal needs write access to the workspace
  tables and to create the DCE and DCR in the function's resource group.
- The Terraform module gains the `azapi` provider for custom tables, which azurerm cannot manage.
- Ops suppression is removed along with its blob container and role. Every Ops run writes a row per hot
  resource, so the table records how long a resource stays hot. De-duplication moves into alert rules.
  The app now keeps no state of its own.
- FinOps runs on its own daily timer (`finops_schedule_cron`), writing one row per cold resource per
  day, and the 14-day metrics query no longer runs every 15 minutes. `schedule_cron` is renamed
  `ops_schedule_cron`.
- The Markdown report's footer (ignored RGs, excluded resources, skip counts) now lives in the
  `run complete` log. Tag hygiene is queryable through `MissingTags`.
- The "report hosting" and "email transport" backlog items are superseded.
