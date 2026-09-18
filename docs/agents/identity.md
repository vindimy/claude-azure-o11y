# Identity and secrets

Read this before adding an Azure call, a secret, or anything else that needs a permission.

## The UAMI

- The runtime identity is one user-assigned managed identity. The bank's IAM automation repo creates and
  manages it. This repo cannot create identities or role assignments. Both deployment paths take the
  UAMI **name** and only attach it to the Function App.
- Authentication is `DefaultAzureCredential` with `AZURE_CLIENT_ID` set to the UAMI client ID. Locally,
  it is `az login`.

## Permissions workflow

This repo is the source of truth for what the UAMI needs.

1. Add a row to `identity/role-requirements.yaml` (role, scope type, scope expression, purpose). Pick the
   narrowest role and scope that work, one role per need.
2. Run `make identity-doc` to regenerate `docs/identity-requirements.md`, the current matrix.
3. Send the YAML to the IAM repo as the request. Write the code only after that.

A need beyond the current matrix (e.g. a Cost Management reader for actual costs) also needs an ADR in
`docs/adr/`. Pushing images belongs to the GitLab runner, not the UAMI; that grant sits outside this repo.

## Self-check

On first run, and through `scripts/check-identity.sh`, verify each YAML row with a cheap read call. Log
the rows that are missing, so a half-provisioned UAMI fails with a message like "missing Log Analytics
Reader on <LAW id>" instead of a generic 403 deep inside a loop.

## Secrets

SMTP credentials, Teams/Power Automate webhook URLs, and future API keys (e.g. Datadog) live in the
existing Key Vault. The Function App reads them through Key Vault references. Keep them out of app
settings as plaintext and out of Terraform state as literals.
