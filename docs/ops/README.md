# Operations runbooks

Install and day-to-day operation of the o11y alerting function, one runbook per deployment style.
For design and rationale see [docs/agents/deployment.md](../agents/deployment.md); these pages are the
"what do I type" companion.

| Runbook | Use it for | Runs as |
|---------|-----------|---------|
| [local-mac.md](local-mac.md) | development, dry runs against a real subscription, ad-hoc live runs | your `az login` user, `python src/run_local.py` |
| [azure-function.md](azure-function.md) | the production service: custom-container Function App on Elastic Premium (Path A `deploy.sh`, Path B Terraform) | the UAMI, two timer functions |
| [azure-vm.md](azure-vm.md) | the same pipeline on a RHEL 9 VM when a Function App is not an option (Path C, Ansible) | the UAMI, two systemd timers |

## What every style has in common

- **Same code, same settings.** All three run `src/` with the settings in the
  [shared parameter contract](../agents/deployment.md#shared-parameter-contract). Only the scheduler
  differs: Functions timers, systemd timers, or you.
- **Same identity model.** The UAMI `id-o11y-alerting` is created and granted by the external IAM repo,
  from [identity/role-requirements.yaml](../../identity/role-requirements.yaml). Nothing in this repo
  creates identities or role assignments. The current matrix is
  [docs/identity-requirements.md](../identity-requirements.md).
- **Same findings path.** Findings go to two custom tables in the LAW named by `law_resource_id`,
  through a DCE and DCR that the deployment creates in the resource group. The
  `findings_ingest` role (`Monitoring Metrics Publisher` on that DCR) can only be granted **after** the
  first deploy, because the DCR does not exist before it.
- **Same inputs.** Existing resource group, UAMI, Log Analytics workspace, and (Function App only)
  storage account, Key Vault, and ACR. None of the runbooks create those.

## First-time order of operations

1. IAM repo: create the UAMI and grant every row of `identity/role-requirements.yaml` except
   `findings_ingest` (the RHEL VM needs neither `host_storage` nor `acr_pull`).
2. Deploy once with the runbook for your style. The deploy prints the findings DCR resource ID.
3. IAM repo: grant `findings_ingest` (`Monitoring Metrics Publisher`) on that DCR ID.
4. Wait up to about 30 minutes for the new assignment to propagate to the Logs Ingestion API
   ([gotchas](../gotchas.md#logs-ingestion-new-dcr-role-assignments-and-first-rows-are-slow-2026-09-17)). Runs in
   that window fail with `missing Monitoring Metrics Publisher on data_collection_rule …`.
5. Trigger a run by hand and confirm rows with the query below.

## Confirming the function works

Every run ends with a structured `run complete` log line, JSON with `mode`, `run_id`, `inventory_total`,
`evaluated`, `findings`, `rows_written`, `skips`, `ignored_rg_count`, `excluded`, and `write_failures`.
Where to read it is in each runbook. Rows in the workspace, once the first ones become queryable
(several minutes on a new table):

```kusto
union O11yOpsFindings_CL, O11yFinOpsFindings_CL
| where TimeGenerated > ago(1d)
| summarize Rows = count(), Last = max(TimeGenerated) by Type, RunId
| order by Last desc
```

## Changing behaviour without redeploying code

| Change | Where | Then |
|--------|-------|------|
| thresholds, windows, tag names, metrics per type | `config/thresholds/default.yaml` or `config/thresholds/<mg-id>.yaml` | new image or release (config is packaged with the code) |
| which resource types run | `RESOURCE_TYPES=vm,sqldb` app setting / env (default: every configured type) | restart |
| ignore resource groups | `config/ignore.yaml` | new image or release |
| one resource's threshold | tag `o11y-threshold-<metric>-hot=95` / `o11y-threshold-<metric>-cold=10` on the resource (`<metric>` is the key in the thresholds file: `cpu`, `memory`, `dtu`, `ru`, …) | next run |
| skip one resource | tag `o11y-exclude=true` on the resource | next run |
| schedule, scope, dry run | `deploy.env` / `terraform.tfvars` / `vm.env`, then re-run the deploy with the same tag | restart is part of the deploy |
