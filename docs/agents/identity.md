# Identity and secrets

Read this before adding an Azure call, a secret, or anything else that needs a permission.

## The UAMI

- The runtime identity is one user-assigned managed identity. The bank's IAM automation repo creates and
  manages it. This repo cannot create identities or role assignments. Every deployment style takes the
  UAMI **resource ID** (`uami_resource_id`), so it may live in any RG or subscription. The Function App
  paths only attach it to the Function App. The RHEL VM path (Path C) expects it already attached to the
  VM; it resolves the ID to a client ID and needs neither `host_storage` nor `acr_pull`.
- `terraform/examples/iam-uami` is a **reference** for the IAM repo: a UAMI plus one role assignment per
  row of `identity/role-requirements.yaml`, read from the YAML. Nothing here applies it, and
  `module-azure-o11y` must never call it.
- Authentication is `DefaultAzureCredential` with `AZURE_CLIENT_ID` set to the UAMI client ID. Locally,
  it is `az login`.

## Permissions workflow

This repo is the source of truth for what the UAMI needs.

1. Add a row to `identity/role-requirements.yaml` (role, scope type, scope expression, purpose). Pick the
   narrowest role and scope that work, one role per need.
2. Run `make identity-doc` to regenerate `docs/identity-requirements.md`, the current matrix.
3. Send the YAML to the IAM repo as the request. Write the code only after that.

A need beyond the current matrix (e.g. a Cost Management reader for actual costs) also needs an ADR in
`docs/adr/`.

`findings_ingest` (`Monitoring Metrics Publisher` on the findings DCR) is scoped to a resource that the
deployment creates. On a fresh environment, grant everything else, deploy, then grant `findings_ingest` on
the `findings_dcr_id` output. Until then, runs fail with `missing Monitoring Metrics Publisher on
data_collection_rule …`. Pushing images belongs to the GitLab runner, not the UAMI; that grant sits outside this repo.

## Self-check

`scripts/check-identity.sh` (run by `deploy.sh` and `vm-install.sh`) verifies each YAML row with a cheap
read call and lists the missing ones. A row whose scope input was not passed (`--storage-account-id`,
`--key-vault-id`, `--acr-name`, `--law-resource-id`, `--findings-dcr-id`) is skipped, and `--skip id,id`
marks rows a deployment style does not need, so the VM path checks only what applies to it. At runtime, a thin client that gets a 403 raises `PermissionMissing("<need id>")`, and
`bootstrap.py` logs it with that row of `identity/role-requirements.yaml`. A half-provisioned UAMI then
fails with a named permission instead of a generic 403 deep inside a loop. New thin clients follow the
same pattern.

## Secrets

No secret is read today; findings ingestion uses the UAMI. Future secrets (e.g. SMTP credentials or a
Datadog API key) live in the existing Key Vault. The Function App reads them through Key Vault references. Keep them out of app
settings as plaintext and out of Terraform state as literals.
