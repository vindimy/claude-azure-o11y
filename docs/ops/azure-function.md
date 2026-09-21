# Azure Function App

The production deployment: a Linux custom-container Function App on an Elastic Premium plan, with two
timer functions (`o11y_ops`, `o11y_finops`) and the UAMI attached. Two interchangeable tools deploy
it, both taking the [same parameters](../agents/deployment.md#shared-parameter-contract):

- **Path A, `scripts/deploy.sh`:** `az` CLI, idempotent. For dev and test environments, or when CI is
  unavailable.
- **Path B, Terraform:** `terraform/module-azure-o11y`, applied by the GitLab pipeline. For prod.

Neither builds an image. A deploy is "point the app at an image tag that already exists in the ACR".

## Prerequisites

Existing, in the target subscription unless noted:

| Resource | Parameter | Notes |
|----------|-----------|-------|
| resource group | `resource_group_name` | receives plan, app, DCE, DCR |
| UAMI | `uami_name` | from the IAM repo; roles per `identity/role-requirements.yaml` |
| storage account | `storage_account_name` | Functions host state only; UAMI needs `Storage Blob Data Owner` |
| Key Vault | `key_vault_name` | no secret referenced today; UAMI needs `Key Vault Secrets User` |
| Log Analytics workspace | `law_resource_id` | may be in another subscription; receives the two tables |
| ACR | `acr_name` | UAMI needs `AcrPull`; the person or runner pushing needs `AcrPush` |
| App Insights (optional) | `app_insights_name` | in the same RG; enables the `traces` queries below |

The deploying principal (you, or the Terraform SP) needs Contributor on the resource group, table write on
the LAW, and read on the UAMI, storage, Key Vault, and ACR.

Workstation tooling for Path A: `az` logged in and set to the target subscription. Add Docker Desktop
only if you build images locally. See [local-mac.md](local-mac.md#tools).

## Build the image

CI does this on every push: `build` pushes `<acr>/o11y-alerting:<commit sha>` and, on `main`, a
moving `main` tag. Deploy the SHA tag, never `main`.

Without CI, from a clean checkout of the commit you want:

```bash
cp scripts/deploy.env.example deploy.env       # ACR_NAME and IMAGE_NAME come from here
scripts/build-image.sh --param-file deploy.env                   # docker build + push, prints IMAGE_TAG=<sha>
scripts/build-image.sh --param-file deploy.env --builder acr     # az acr build; no local Docker
scripts/build-image.sh --param-file deploy.env --deploy          # build, push, then deploy.sh with that tag
scripts/build-image.sh --param-file deploy.env --allow-dirty     # uncommitted tree -> dev-<sha>-<timestamp> tag
```

The script refuses a dirty tree without `--allow-dirty`, refuses to overwrite an existing tag without
`--force`, and refuses to build while `build/base-image.txt` still holds the placeholder. `make image
PARAMS=deploy.env ARGS=--deploy` is the same thing.

## Install and update: Path A (`deploy.sh`)

```bash
cp scripts/deploy.env.example deploy.env
$EDITOR deploy.env            # at least: RESOURCE_GROUP_NAME LOCATION UAMI_NAME STORAGE_ACCOUNT_NAME
                              #           KEY_VAULT_NAME MANAGEMENT_GROUP_ID LAW_RESOURCE_ID ACR_NAME IMAGE_TAG
scripts/deploy.sh --param-file deploy.env
```

Flags override the file and take the lower-case, dashed form of the parameter name:

```bash
scripts/deploy.sh --param-file deploy.env --image-tag 4b466942…      # deploy a specific build
scripts/deploy.sh --param-file deploy.env --dry-run true             # switch the app to dry-run mode
scripts/deploy.sh --param-file deploy.env --ops-schedule-cron "0 */30 * * * *"
scripts/deploy.sh --param-file deploy.env --skip-identity-check      # when you cannot list role assignments
```

What a run does, in order: verify the tag exists in the ACR, resolve the UAMI/storage/KV/LAW, create or
update the findings tables, DCE, and DCR, create the plan and app if missing, attach the UAMI and
configure managed-identity pull, set app settings, run `scripts/check-identity.sh`, restart the app,
print the host name, DCR ID, and a manual-trigger command. Re-running with the same inputs changes
nothing except the restart.

**First deploy:** `check-identity.sh` will report `MISSING findings_ingest` because the DCR did not
exist until now. Send the printed DCR ID to the IAM repo, then wait up to 30 minutes.

**Update:** re-run with the new `--image-tag`. That is the whole deploy.

**Rollback:** re-run with the previous tag. Tags are immutable commit SHAs, so any earlier build is
still in the ACR.

## Install and update: Path B (Terraform)

The pipeline: `lint` → `build` → `plan` (with `-var image_tag=$CI_COMMIT_SHA`) → `apply` (manual,
`main` only, consumes the plan artifact). Merging to `main` and pressing `apply` is the deploy. Rollback
is re-running `apply` from an older pipeline, or a revert commit.

From a workstation, for a test environment:

```bash
cd terraform/examples/test-rg
cp terraform.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars   # same keys as deploy.env
terraform init
terraform plan -var image_tag=<sha> -out=tfplan
terraform apply tfplan
terraform output findings_dcr_id                 # give this to the IAM repo for findings_ingest
```

The example uses a local backend; state stays in that directory. Changing only `image_tag` produces a
one-attribute diff on the function app, and that diff is the code deploy. Do not run `deploy.sh`
against a Terraform-managed app; the two do not share state.

## Operate

### Check it is running

```bash
az functionapp show -g rg-o11y-test -n func-o11y-alerting --query "{state:state, image:siteConfig.linuxFxVersion}" -o table
az functionapp function list -g rg-o11y-test -n func-o11y-alerting --query "[].{name:name, disabled:isDisabled}" -o table
```

Both functions should be listed and enabled a minute or two after a restart. If the list is empty, the
container has not started; see the log stream.

### Trigger a run now

Timer functions are invoked through the admin endpoint with the master key:

```bash
RG=rg-o11y-test; APP=func-o11y-alerting
KEY=$(az functionapp keys list -g $RG -n $APP --query masterKey -o tsv)
HOST=$(az functionapp show -g $RG -n $APP --query defaultHostName -o tsv)
curl -X POST "https://$HOST/admin/functions/o11y_ops"    -H "x-functions-key: $KEY" -H "Content-Type: application/json" -d '{}'
curl -X POST "https://$HOST/admin/functions/o11y_finops" -H "x-functions-key: $KEY" -H "Content-Type: application/json" -d '{}'
```

A `202 Accepted` means queued; the run itself takes from seconds to minutes depending on inventory
size. A timer never overlaps its own previous run.

### Read the logs

Live stream, JSON lines from the worker:

```bash
az webapp log tail -g rg-o11y-test -n func-o11y-alerting
```

With App Insights, the structured lines are in `traces` with the JSON in `message`:

```kusto
traces
| where timestamp > ago(1d) and message has "run complete"
| extend j = parse_json(message)
| project timestamp, mode = j.mode, run_id = j.run_id, inventory = j.inventory_total,
          evaluated = j.evaluated, findings = j.findings, written = j.rows_written,
          write_failures = j.write_failures, skips = j.skips
| order by timestamp desc
```

```kusto
traces
| where timestamp > ago(1d) and (severityLevel >= 3 or message has "PermissionMissing")
| project timestamp, message
```

Then confirm rows landed with the query in [README.md](README.md#confirming-the-function-works).

### Change settings

Schedules, scope, dry run, and App Insights are app settings. Change them by editing `deploy.env` or
`terraform.tfvars` and re-running the deploy with the **same** image tag; the deploy restarts the app.
Editing settings in the portal works until the next deploy overwrites them, so do not.

A change to `config/` (thresholds, ignore list, assignment groups) is a code change: commit, build a
new tag, deploy it.

### Pause and resume

```bash
az functionapp stop  -g rg-o11y-test -n func-o11y-alerting     # no runs until started
az functionapp start -g rg-o11y-test -n func-o11y-alerting
```

Missed schedules are not caught up (`run_on_startup=False`). To pause one mode only, deploy with an
NCRONTAB that never fires, for example `0 0 0 31 2 *`.

### Verify the identity

```bash
scripts/check-identity.sh --uami-name id-o11y-alerting --resource-group rg-o11y-test \
  --management-group-id mg-prod \
  --storage-account-id $(az storage account show -g rg-o11y-test -n sto11yalerting --query id -o tsv) \
  --key-vault-id $(az keyvault show -n kv-o11y-alerting --query id -o tsv) \
  --acr-name acrusbcloud --law-resource-id "$LAW_RESOURCE_ID" \
  --findings-dcr-id "$(az group show -n rg-o11y-test --query id -o tsv)/providers/Microsoft.Insights/dataCollectionRules/dcr-o11y-findings"
```

Prints `ok` or `MISSING` per row; exit code 1 if anything is missing. `deploy.sh` runs it for you.

### Remove

Path A removes only what it created:

```bash
scripts/destroy.sh --param-file deploy.env                 # app, plan, DCR, DCE; tables and data stay
scripts/destroy.sh --param-file deploy.env --with-tables   # also the two tables and all their rows
```

Path B: `terraform destroy` in the example directory. The module manages the tables, so destroy deletes
them and their data. The RG, UAMI, storage, Key Vault, LAW, and ACR are never touched by either.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `image tag … not found in <acr>/o11y-alerting` | deploying a SHA that CI has not built, or a typo | check `az acr repository show-tags`; build it |
| function list empty, log stream shows pull errors | UAMI lacks `AcrPull`, or the image is arm64 | `check-identity.sh`; rebuild with `build-image.sh` (forces amd64) |
| host fails to start with storage errors | UAMI lacks `Storage Blob Data Owner` on the storage account | it needs the account-scoped role; container-scoped is not enough |
| `missing Monitoring Metrics Publisher on data_collection_rule …` | `findings_ingest` not granted, or granted under 30 minutes ago | grant on the DCR ID from the deploy output; wait |
| `LOGS_INGESTION_ENDPOINT is required when DRY_RUN=false` | app settings missing the ingestion values | re-run the deploy; it resolves them from the DCE/DCR |
| `table … not provisioned (state: …)` during deploy | the async table PUT did not reach `Succeeded` in 5 minutes | re-run; if it persists, check the LAW's provisioning state and your table-write rights |
| DCR PUT rejected, output stream unknown | DCR created before its table existed | re-run the deploy; it waits for the table first |
| `PLAN_SKU must be Elastic Premium or Dedicated` | Consumption cannot run custom containers | use `EP1`+ |
| rows missing for a hot VM you expected | resource excluded by tag, RG ignored, or under 50% metric coverage | look at `excluded`, `ignored_rg_count`, and `skipped resource` lines |
| runs stop after a while, no errors | plan scaled to zero or app stopped | `az functionapp show --query state`; EP plans keep one instance warm |
| `deploy.sh` loaded no parameters | old macOS bash quirk, fixed; or the file has `KEY = value` with spaces | use `KEY=value`, one per line |
