# Dashboards, alerts, and Teams (module-o11y-insights)

The "what do I type" companion to [docs/agents/insights.md](../agents/insights.md). This runbook installs
the Ops and FinOps workbooks, the log search alerts, and Teams delivery on top of the findings tables. It
works the same whichever style runs the pipeline (Function App or RHEL VM).

## Prerequisites

- **The pipeline already writes rows.** Alert queries are validated against the workspace at `apply`,
  so both `O11yOpsFindings_CL` and `O11yFinOpsFindings_CL` must exist. Confirm with the query in
  [README.md](README.md#confirming-the-function-works).
- **Inputs:** an existing resource group, the workspace (`law_resource_id`), and optionally the App
  Insights component the pipeline logs to (`app_insights_id`; full resource ID). For Teams you also need
  a Key Vault (`key_vault_id`) and a user-assigned identity (`uami_resource_id`). The RHEL VM style has no
  Key Vault of its own; use any vault the IAM repo can grant on.
- **Identity (IAM repo):** the identity in `uami_resource_id` needs two existing rows of
  [identity/role-requirements.yaml](../../identity/role-requirements.yaml): `law` (Log Analytics Reader on
  the workspace, for the FinOps digest) and `secrets` (Key Vault Secrets User on the vault, for the
  webhook URLs). The function's UAMI usually has `law` already; check `secrets` is granted on **this**
  vault.
- **You (the deploying principal):** create rights in the RG for workbooks, alert rules, action groups,
  and Logic Apps; read on the workspace, App Insights, and vault; and Managed Identity Operator on the
  UAMI, so it can be assigned to the Logic Apps.
- Terraform ≥ 1.9 and `az` logged in.

## 1. Create a Teams workflow per channel

Office 365 connectors (incoming webhooks) were retired from Teams in May 2026; a channel now receives
posts through a Workflows webhook. For each channel that should receive cards:

1. In Teams, open the channel → **⋯** → **Workflows**.
2. Pick the template **Send webhook alerts to a channel**, or **Post to a channel when a webhook request
   is received**. Choose the team and channel, then finish.
3. Copy the URL the last step shows. It contains a signature: treat it as a secret.

Posts appear from the Workflows bot.

## 2. Store each URL in Key Vault

Use the secret names you will put in `teams_secret_name` (letters, digits, hyphens; at most 60 chars):

```bash
read -rs TEAMS_URL && printf %s "$TEAMS_URL" > /tmp/teams-url && unset TEAMS_URL
az keyvault secret set --vault-name kv-o11y-alerting --name teams-cloud-ops --file /tmp/teams-url
rm /tmp/teams-url
```

The Logic Apps read the latest version on every run, so rotating a URL is just a new secret version, with
no apply.

## 3. Configure and apply

Pick one of the three ways to run it. All of them apply the same root, `terraform/examples/insights`.

### From a package (no repository needed)

The release's Azure package, `releases/o11y-insights-v<N>.tar.gz`, holds the module, the root, this
runbook, and `install.sh`. The installer keeps the Terraform state and your `terraform.tfvars` in a
directory **outside** the versioned package directory, so the next package updates the same resources:

```bash
sha256sum -c o11y-insights-v6.tar.gz.sha256
tar xzf o11y-insights-v6.tar.gz && cd o11y-insights-v6
./install.sh --state-dir ~/o11y-insights            # first run: writes ~/o11y-insights/terraform.tfvars and stops
$EDITOR ~/o11y-insights/terraform.tfvars
./install.sh --state-dir ~/o11y-insights            # init, plan, confirm, apply, then print the outputs
./install.sh --state-dir ~/o11y-insights --plan-only
./install.sh --state-dir ~/o11y-insights --destroy  # removes the module's resources; findings tables stay
```

To update, extract a newer package and run its `install.sh` with the **same** `--state-dir`. To roll
back, run an older package's `install.sh` the same way. `--yes` skips the confirmation, and anything
after `--` goes to `terraform plan`. Keep `--state-dir` backed up, or switch the root's `versions.tf` to
an `azurerm` backend when several people apply. Build a package with `make insights-package VERSION=<n>`
([releases](../../releases/README.md)).

### From the repository

```bash
cd terraform/examples/insights
cp terraform.tfvars.example terraform.tfvars     # git-ignored
$EDITOR terraform.tfvars
terraform init
terraform plan -out=tfplan
terraform apply tfplan
terraform output workbook_urls
terraform output teams_secret_names             # every one must exist in the vault
```

### From CI

Add a root like `terraform/examples/insights` with a remote backend and plan/apply it the same way as the
function's root. The `lint:terraform` job already validates and tests this module.

### Settings

What to set in `terraform.tfvars`:

| Variable | Meaning |
|---|---|
| `routes` | Map of route → `assignment_groups` it claims, `catch_all` (exactly one route), `ops = { teams_secret_name, emails, severity }`, `finops = { teams_secret_name, emails, digest, new_saving_alert }`. Set `ops` or `finops` to `null` (or leave it out) to turn it off for that route. |
| `health` | `{ teams_secret_name, emails }` for the pipeline-health alerts. |
| `ops_cadence_minutes` | Must match `ops_schedule_cron` (default every 15 minutes). |
| `ops_sustained_runs` | Consecutive runs a pair must stay hot before it alerts (default 2, so about 30 minutes). |
| `finops_new_saving_min` | Smallest monthly saving that counts as a new candidate in the daily alert (default 100). |
| `finops_max_age_hours` | Hours without a FinOps run before the health alerts fire (default 26). |
| `finops_digest_schedule` | `{ frequency = "Week" or "Day", interval, week_days, hour, minute }`, UTC (default Monday 14:00). The period is at most 30 days. |
| `alerts_enabled` | `false` creates the rules disabled. Use it for a first look at the workbooks. |
| `runbook_url` | Optional link shown on every card. |

`AssignmentGroup` values are the `assignment_group` tag on the resources (see
[findings](../agents/findings.md#ownership-tags)). The Ops workbook's **Routing gaps** grid and the
FinOps workbook's **Missing ownership tags** grid list what falls to the catch-all route.

## 4. Verify

**Workbooks:** open each link from `terraform output workbook_urls`. The Ops workbook shows *Hot now*
from the latest run. The FinOps workbook needs one daily run within the last 2 days.

**A Teams channel end to end**, without waiting for an alert. This posts the sample Ops card through
the same Logic App the action groups call. Run it from the repository root or the package directory:

```bash
RG=rg-o11y-test; SECRET=teams-cloud-ops
ID=$(az resource show -g $RG -n logic-o11y-teams-$SECRET --resource-type Microsoft.Logic/workflows --query id -o tsv)
URL=$(az rest --method post --url "https://management.azure.com$ID/triggers/manual/listCallbackUrl?api-version=2019-05-01" --query value -o tsv)
curl -sS -X POST -H 'Content-Type: application/json' \
  -d @terraform/module-o11y-insights/samples/common-alert-ops.json "$URL" -w '%{http_code}\n'   # 202
```

**The FinOps digest now** (it otherwise waits for its schedule):

```bash
ID=$(az resource show -g $RG -n logic-o11y-finops-digest-platform --resource-type Microsoft.Logic/workflows --query id -o tsv)
az rest --method post --url "https://management.azure.com$ID/triggers/Recurrence/run?api-version=2019-05-01"
```

**Through an action group**, including email: Portal → Monitor → Alerts → Action groups →
`ag-o11y-ops-<route>` → **Test** → *Log search alert (V2)*.

Then check the Logic App's **Run history**. Every step should be green, and the two webhook steps show
"secured" instead of the URL.

## Day to day

| Change | Do |
|---|---|
| New team or channel | Create the workflow and secret (steps 1–2), add or extend a route, apply |
| Move a group to another route | Edit `assignment_groups`, apply. A group may sit in one route only |
| Rotate a webhook URL | `az keyvault secret set` with the new URL; no apply |
| Too many Ops cards | Raise `ops_sustained_runs`, or tune thresholds in `config/thresholds/` (the source of every finding) |
| Silence one resource | Tag `o11y-exclude=true`, or raise its `o11y-threshold-<metric>-hot` tag ([thresholds](../agents/thresholds.md)) |
| Pause all alerts | `alerts_enabled = false`, apply |
| Remove | `terraform destroy` in the root. The findings tables and their data are untouched |

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `apply` fails on an alert rule with a query validation error | a findings table does not exist yet in the workspace | let the pipeline write its first rows (both modes), then re-apply |
| Logic App run fails at `Get_webhook_url` with 403 | identity lacks `secrets` on this vault | IAM repo: grant Key Vault Secrets User (vault must use Azure RBAC) |
| `Get_webhook_url` 404 | secret name in tfvars not in the vault | `terraform output teams_secret_names`; create the missing secret |
| Digest fails at `Summary_query` / `Top_query` with 403 | identity lacks `law` | IAM repo: grant Log Analytics Reader on the workspace |
| `Post_to_Teams` 400/404 | URL is not a Teams Workflows webhook, or the workflow was deleted or turned off in Teams | recreate the workflow (step 1), update the secret |
| `Post_to_Teams` 202 but nothing in the channel | the workflow's own run failed in Teams | Teams → Workflows → the flow → run history (often: the flow owner left the team) |
| `health-run-missing` fires on the VM | runs are healthy but not exported to App Insights | set `app_insights_name` in `vm.env` and re-run the installer, or drop `app_insights_id` here |
| `health-finops-stale` fires but runs are healthy | no resource is cold anywhere in scope | `finops_stale_alert_enabled = false` |
