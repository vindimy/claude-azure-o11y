# Deployment

Read this before touching `Dockerfile`, `scripts/deploy.sh` / `destroy.sh`, `terraform/`, or
`.gitlab-ci.yml`, or before adding a deployment parameter.

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
this table in the same PR. `tests/test_param_parity.py` diffs all four.

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
