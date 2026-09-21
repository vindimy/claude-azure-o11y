# claude-azure-o11y

Scheduled Azure Function (Python, custom container) that evaluates built-in Azure Monitor metrics across
every subscription under a management group and writes **Ops findings** (hot resources →
`O11yOpsFindings_CL`) and **FinOps findings** (cold resources with a downsizing recommendation and saving →
`O11yFinOpsFindings_CL`) to a Log Analytics workspace. MVP scope: VM `Percentage CPU`. Table schema:
[schema/findings-tables.json](schema/findings-tables.json) · [docs/agents/findings.md](docs/agents/findings.md).

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
written to Log Analytics, rows land in `./out/findings/<table>.jsonl`):

```bash
scripts/run-once.sh --mg-id mg-nonprod --subscription-ids "<subscription-id>" [--mode ops|finops|all]
scripts/run-once.sh --param-file deploy.env --live --mode finops   # write to the workspace instead
```

## Deploying

Operator runbooks (install, run now, logs, update, rollback, remove) per style: [docs/ops/](docs/ops/).

The image is built and pushed by GitLab CI (`.gitlab-ci.yml`) as `<acr>/o11y-alerting:<commit sha>`.
Without CI, `scripts/build-image.sh --param-file deploy.env` builds and pushes the same tag, and `--deploy`
then runs `deploy.sh` ([details](docs/agents/deployment.md#building-and-pushing-the-image-manually)).
Both deployment paths take the same parameters and only reference that tag.

- **Path A (dev, az CLI):** `cp scripts/deploy.env.example deploy.env`, fill it in, then
  `scripts/deploy.sh --param-file deploy.env --image-tag <sha>`. Re-run with a new tag to deploy.
- **Path B (CI, Terraform):** `terraform/module-azure-o11y`, exercised by `terraform/examples/test-rg`.
  A new `image_tag` is the deploy; the pipeline's `apply` stage is manual.
- **Path C (RHEL 9 VM, Ansible):** after the VM is provisioned with the UAMI attached,
  `cp scripts/vm.env.example vm.env`, fill it in, then `scripts/vm-install.sh --param-file vm.env`.
  The same code runs from systemd timers. Re-run with `--release-ref <sha>` to update
  ([details](docs/agents/deployment.md#path-c-rhel-9-vm-scriptsvm-installsh--ansible)).

The runtime identity is an existing user-assigned managed identity. What it must be granted is listed in
`identity/role-requirements.yaml`; `scripts/check-identity.sh` verifies it. `terraform/examples/iam-uami`
is a reference definition of that UAMI and its role assignments for the IAM repo.

## Configuration

`config/thresholds/default.yaml` (per-MG overrides in `config/thresholds/<mg-id>.yaml`), `config/ignore.yaml`
(resource-group regex ignore list), `config/assignment-groups.yaml`, `config/vm-skus.yaml`.
Per-resource tags `o11y-threshold-cpu-hot=95` / `o11y-threshold-cpu-cold=10` override thresholds; `o11y-exclude=true`
skips a resource (it is still listed in the run's `run complete` log).
