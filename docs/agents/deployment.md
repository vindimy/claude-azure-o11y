# Deployment

Operator runbooks live in [docs/ops/](../ops/README.md); keep them in step with any change here.

Read this before touching `Dockerfile`, `scripts/deploy.sh` / `destroy.sh` / `vm-install.sh`, `terraform/`,
`ansible/`, or `.gitlab-ci.yml`, or before adding a deployment parameter.

## Container image

The function runs as a custom Linux container, the established US Bank pattern.

- `Dockerfile` builds `FROM` the approved base pinned in `build/base-image.txt`. Pull base images only
  from the approved registry, never from Docker Hub or MCR directly.
- The image holds `src/`, the installed `requirements.txt`, and `host.json`. It holds no secrets and no
  environment-specific config; everything comes from app settings at runtime.
- Image name: `o11y-alerting`. Tags are `$CI_COMMIT_SHA` (immutable) and a moving `main`. Deployments
  always pin the SHA tag.
- GitLab CI builds and pushes the image (`build` job). `scripts/build-image.sh` is the manual equivalent;
  see below.

## Building and pushing the image manually

Use this when CI is unavailable, or for a dev deploy through `deploy.sh`. The script does what the CI
`build` job does: it builds `FROM` the approved base in `build/base-image.txt` and pushes
`<acr>/<image_name>:<tag>`.

```bash
cp scripts/deploy.env.example deploy.env              # ACR_NAME and IMAGE_NAME are read from here
scripts/build-image.sh --param-file deploy.env        # build + push; prints IMAGE_TAG=<sha>
scripts/build-image.sh --param-file deploy.env --deploy   # ...then scripts/deploy.sh with that tag
make image PARAMS=deploy.env ARGS=--deploy            # same, via make
```

- **Tag:** the full commit SHA by default, identical to CI's `$CI_COMMIT_SHA`, so a manual and a CI build
  of one commit are interchangeable. The script refuses a dirty working tree; `--allow-dirty` builds a
  `dev-<short sha>-<UTC timestamp>` tag instead. It also refuses to overwrite a tag that already exists
  in the ACR (`--force` overrides).
- **Builder:** `--builder docker` (default) needs a local Docker daemon and `docker login` to the approved
  base registry. It always builds `linux/amd64`; Functions on Linux cannot start an arm64 image built on
  Apple Silicon. `--builder acr` runs `az acr build` inside the registry, with no local Docker; the ACR
  must be able to pull the base image.
- **Rights:** the person running it needs `AcrPush` on the ACR (and ACR Tasks allowed for
  `--builder acr`). The UAMI only ever pulls.
- The script never deploys unless `--deploy` is passed, and `deploy.sh` still never builds.

## Two independent paths

Both paths deploy the whole stack into an existing RG: an Elastic Premium plan, a Function App with the
UAMI attached and the image configured, app settings, and the findings path.
The findings path is two custom tables in the existing LAW, plus a DCE and DCR in the RG, located in the
LAW's region. All of it is rendered from `schema/findings-tables.json` ([findings](findings.md)). Both only **reference** an existing image tag. They never build, push, or zip-deploy
anything, and neither needs a Docker daemon.

- Neither path creates the RG, UAMI, storage account, Key Vault, LAW, or ACR. Those are inputs.
- The deploying principal needs table write on the LAW (it may be in another subscription) and DCR/DCE
  create rights in the RG.
- Neither path calls the other: `deploy.sh` never runs Terraform, and the module never runs the script.

### Path A: `scripts/deploy.sh` (dev)

- Pure `az` CLI and idempotent. Parameters come from `--param-file deploy.env` or flags; flags win.
- Steps: validate inputs → confirm `image_tag` exists (`az acr repository show-tags`) → resolve the
  UAMI, storage, KV, and LAW → PUT the findings tables, DCE, and DCR (`az rest`, pinned API versions) →
  create or update the plan and function app with `--image` and
  `--acr-identity` → attach the UAMI → set app settings → run
  `check-identity.sh` → print the invoke URL. Re-running with a new `image_tag` is the deploy.
- `destroy.sh` removes only what `deploy.sh` created: the plan, the function app, and the DCR and DCE. It
  removes the tables (with their data) only with `--with-tables`.
- The scripts run on macOS's stock bash 3.2 as well as bash 4+; parameter parsing lives in
  `scripts/lib/params.sh`.

### Path B: `terraform/module-azure-o11y` (CI, prod)

- Use the latest Terraform, `azurerm` 4.x, and `azapi` 2.x (only for the LAW custom tables, which
  azurerm cannot manage). Keep separate `data.tf`, `locals.tf`, `variables.tf`,
  `main.tf`, and `outputs.tf`. `terraform/examples/test-rg/` calls the module end to end with a local
  backend.
- `azurerm_linux_function_app` pulls the image through the UAMI:
  `site_config.application_stack.docker { registry_url, image_name, image_tag }`,
  `container_registry_use_managed_identity = true`, and
  `container_registry_managed_identity_client_id`. Use no admin credentials and no
  `DOCKER_REGISTRY_SERVER_*` settings.
- A new `image_tag` is a one-attribute plan diff; that diff is the code deploy.
- The module contains only infrastructure that references things. Identity and role-assignment
  resources, `archive_file`, `zip_deploy_file`, the `docker` provider, and `local-exec` are all
  excluded. The UAMI reference for the IAM repo is the separate root `terraform/examples/iam-uami`.
- Upgrading from the Teams/report version: a `removed` block drops the old `reports` and `suppression`
  containers from state without deleting them. Delete them by hand when the old reports are no longer
  needed.
- Resource names live only in `locals.tf`, so the naming and tagging standards (still to come) can be
  swapped in one place.

## Shared parameter contract

The names and meanings are identical in both paths: snake_case in Terraform, UPPER_SNAKE in the script.
Adding a parameter means updating `variables.tf`, `deploy.env.example`, the script's `PARAMS=(…)`, and
this table in the same PR. Unless the parameter only makes sense for a Function App, also add it to
Path C (`vm.env.example`, `vm-install.sh`, the role's `argument_specs.yml`, and the Path C table).
`tests/test_param_parity.py` diffs all of them.

| Parameter | Example | Notes |
|-----------|---------|-------|
| `resource_group_name` | `rg-o11y-test` | existing |
| `location` | `centralus` | |
| `uami_name` | `id-o11y-alerting` | existing, from the external IAM repo; resolved to ID + client ID |
| `storage_account_name` | `sto11yalerting` | existing; Functions host storage only |
| `key_vault_name` | `kv-o11y-alerting` | existing; no secret is referenced today (SMTP/Datadog later) |
| `management_group_id` | `mg-prod` | scope of iteration |
| `law_resource_id` | `/subscriptions/…/workspaces/law-central` | existing; **required**. Receives the findings tables; also the VM guest-metrics source later |
| `acr_name` | `acrusbcloud` | existing; the UAMI needs `AcrPull` |
| `image_name` | `o11y-alerting` | |
| `image_tag` | `a1b2c3d` | immutable SHA tag built by CI; **required**, no default |
| `function_app_name`, `app_service_plan_name` | | naming standards TBD; defaults in `locals.tf` / script |
| `plan_sku` | `EP1` | custom containers need Elastic Premium or Dedicated; Consumption is not valid |
| `ops_schedule_cron` | `0 */15 * * * *` | NCRONTAB (UTC) for the Ops run (`o11y_ops`) |
| `finops_schedule_cron` | `0 0 6 * * *` | NCRONTAB (UTC) for the daily FinOps run (`o11y_finops`) |
| `dry_run` | `false` | when true, findings go to JSON lines in the container instead of LAW |
| `subscription_ids` | `` | optional comma list; when set, it replaces the MG as the scope (fallback mode) |
| `app_insights_name` | `` | optional existing App Insights component in the RG; sets `APPLICATIONINSIGHTS_CONNECTION_STRING` |

## GitLab CI

`lint` (ruff, mypy, pytest, `terraform fmt -check`, `terraform validate`) → `build` (docker build + push
`$CI_COMMIT_SHA`) → `plan` (`-var image_tag=$CI_COMMIT_SHA`, plan-file artifact) → `apply` (manual,
protected branch only, consumes the plan artifact).

- `build` must pass before `plan`, so a plan never references a tag that doesn't exist.
- The pipeline authenticates as the Terraform SP, not the UAMI. Its secrets are GitLab CI variables.

## Path C: RHEL 9 VM (`scripts/vm-install.sh` + `ansible/`)

This path runs the same `src/` pipeline on an existing RHEL 9 VM instead of a Function App. It has no
container and no Functions host. Run it after the VM is provisioned, and re-run it to update.

```bash
cp scripts/vm.env.example vm.env                     # or reuse deploy.env; extra keys are ignored
scripts/vm-install.sh --param-file vm.env            # installs HEAD (a clean tree is required)
scripts/vm-install.sh --param-file vm.env --release-ref <sha|tag>   # update or roll back
scripts/vm-install.sh --param-file vm.env -- --private-key ~/.ssh/id_vm   # extra ansible-playbook args
```

- **Assumptions:** the UAMI is already attached to the VM with every row of
  `identity/role-requirements.yaml` except `host_storage` and `acr_pull`, which only a Function App
  uses. Only the operator's workstation needs `az` (logged in), `ansible-core`, and SSH with sudo to
  the VM. The VM needs outbound access to RHUI (dnf), a PyPI index (`PIP_INDEX_URL` for the approved
  mirror), and Azure.
- **What the script does (workstation):** it packs the release with `git archive` (the full commit
  SHA is the release id, the VM's `image_tag`; `--allow-dirty` gives `dev-<sha>-<ts>`). It resolves the
  UAMI client ID and ensures the findings tables, DCE, and DCR through `scripts/lib/findings.sh`, which
  is the same code `deploy.sh` runs. `--no-findings-infra` only looks them up. It runs
  `scripts/check-identity.sh` with `--skip host_storage,acr_pull` (never blocking;
  `--skip-identity-check` turns it off), resolves the App Insights connection string, then runs
  `ansible/playbook.yml` against the `o11y_vm` group. The extra vars travel in a 0600 temp file.
- **What the role does (VM, `ansible/roles/o11y_alerting`):**
  1. Checks for RHEL 9 and a working IMDS token for the UAMI.
  2. Installs `python3.11` (the base image's version) and creates an `o11y` system user.
  3. Unpacks the release to `/opt/o11y-alerting/releases/<id>/` and gives it its own venv
     (`requirements-vm.txt`).
  4. Smoke-tests the import and this MG's threshold config, then switches the `current` symlink. A
     broken release never goes live.
  5. Keeps 3 releases.
- **Runtime:** `/etc/o11y-alerting/o11y-alerting.env` (0640) holds the same settings as the Function
  App. There is one `o11y-alerting-<mode>.timer` per run mode, which starts
  `o11y-alerting@<mode>.service` (oneshot, hardened, `TimeoutStartSec` = the 30-minute
  `functionTimeout`). A timer never overlaps its own run, and a run missed while the VM was off is
  skipped (`Persistent=false`, like `run_on_startup=False`).
- **Schedules:** `ops_schedule_cron` / `finops_schedule_cron` keep their NCRONTAB (UTC) meaning. The
  role's `ncrontab_to_oncalendar` filter converts them to `OnCalendar`, and `systemd-analyze calendar`
  validates the result. An expression that restricts both day-of-month and day-of-week is rejected:
  cron ORs those two fields, and systemd cannot express that.
- **Logs:** JSON to journald (`journalctl -u 'o11y-alerting@*'`). With `app_insights_name` set, the app
  also exports them through `azure-monitor-opentelemetry`; it skips that step under the Functions
  host, which exports logs itself.
- **Dry run:** findings land in `/var/lib/o11y-alerting/out/findings/*.jsonl`.
- No uninstall script yet: stop and disable the timers, then remove `/opt/o11y-alerting`,
  `/etc/o11y-alerting`, and the units. The findings path belongs to `destroy.sh` / Terraform.

### Path C parameters

Contract parameters keep their names and meanings. `vm-install.sh` uses UPPER_SNAKE (in `vm.env` or as
flags), and the role uses snake_case. The Function-App-only parameters (`location`,
`storage_account_name`, `key_vault_name`, `acr_name`, `image_name`, `image_tag`, `function_app_name`,
`app_service_plan_name`, `plan_sku`) are ignored.

| Parameter | Example | Notes |
|-----------|---------|-------|
| `resource_group_name` | `rg-o11y-test` | holds the UAMI and the findings DCE/DCR |
| `uami_name` | `id-o11y-alerting` | resolved to its client ID (`AZURE_CLIENT_ID`); must be attached to the VM |
| `management_group_id` | `mg-prod` | scope; the smoke test validates `config/` with this MG's overrides |
| `subscription_ids` | `` | as in the contract |
| `law_resource_id` | `/subscriptions/…/workspaces/law-central` | **required** |
| `ops_schedule_cron`, `finops_schedule_cron` | `0 */15 * * * *` | NCRONTAB (UTC) → systemd timers |
| `dry_run` | `false` | `true` or `false` |
| `app_insights_name` | `` | optional; enables the OpenTelemetry export |
| `vm_host`, `vm_ssh_user` | `10.0.0.4`, `azureuser` | VM only; or `--inventory` with an `o11y_vm` group |
| `release_ref` | `a1b2c3d` | VM only; commit, tag, or branch (default `HEAD`), recorded as the full SHA |
| `pip_index_url` | `https://artifactory…/simple` | VM only; approved PyPI mirror |
