# claude-azure-o11y

Scheduled Azure Function (Python, custom container) that evaluates built-in Azure Monitor metrics across
every subscription under a management group and produces **Ops alerts** (hot resources → Teams) and a
**FinOps savings report** (cold resources → Markdown in blob storage). MVP scope: VM `Percentage CPU`.

Design and rationale: [CLAUDE.md](CLAUDE.md) → [docs/agents/](docs/agents/) · spec: [docs/superpowers/specs](docs/superpowers/specs/) ·
plan: [docs/superpowers/plans](docs/superpowers/plans/) · surprises: [docs/gotchas.md](docs/gotchas.md) ·
UAMI roles: [docs/identity-requirements.md](docs/identity-requirements.md).

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
make test          # pytest with coverage gate on evaluate/ and recommend/
make lint          # ruff + mypy
make identity-doc  # regenerate docs/identity-requirements.md from identity/role-requirements.yaml
```

Run one evaluation against a real subscription in dry-run mode (uses `az login` credentials; nothing is
sent, the report lands in `./out/reports/<date>/<mg>/virtual-machines.md`):

```bash
scripts/run-once.sh --mg-id mg-nonprod --subscription-ids "<subscription-id>"
```

## Deploying

The image is built and pushed by GitLab CI (`.gitlab-ci.yml`) as `<acr>/o11y-alerting:<commit sha>`.
Both deployment paths take the same parameters and only reference that tag.

- **Path A (dev, az CLI):** `cp scripts/deploy.env.example deploy.env`, fill it in, then
  `scripts/deploy.sh --param-file deploy.env --image-tag <sha>`. Re-run with a new tag to deploy.
- **Path B (CI, Terraform):** `terraform/module-azure-o11y`, exercised by `terraform/examples/test-rg`.
  A new `image_tag` is the deploy; the pipeline's `apply` stage is manual.

The runtime identity is an existing user-assigned managed identity. What it must be granted is listed in
`identity/role-requirements.yaml`; `scripts/check-identity.sh` verifies it.

## Configuration

`config/thresholds/default.yaml` (per-MG overrides in `config/thresholds/<mg-id>.yaml`), `config/ignore.yaml`
(resource-group regex ignore list), `config/routing.yaml`, `config/assignment-groups.yaml`, `config/vm-skus.yaml`.
Per-resource tags `o11y-threshold-cpu-hot=95` / `o11y-threshold-cpu-cold=10` override thresholds; `o11y-exclude=true`
skips a resource (it still appears in the report's excluded appendix).
