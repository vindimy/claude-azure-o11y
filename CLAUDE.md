# CLAUDE.md: Azure Resource Utilization Alerting (Ops + FinOps)

A scheduled Python Azure Function (custom container, v2 model). For every subscription under a management
group, it reads built-in Azure Monitor metrics and writes one row per finding to Log Analytics:

- **Ops findings** for hot resources → `O11yOpsFindings_CL`, on every Ops run (every 15 min by default)
- **FinOps findings** for cold resources, with a downsizing recommendation and estimated saving →
  `O11yFinOpsFindings_CL`, on a daily FinOps run

Alerting and routing are built on those tables downstream; the function sends nothing itself.

MVP is VM CPU only. Owner: Dmitriy (Cloud Engineering).

## Commands

- `make test`: pytest; fails below 80% coverage on `evaluate/` and `recommend/`
- `make lint`: ruff + mypy
- `make identity-doc`: regenerate `docs/identity-requirements.md` from `identity/role-requirements.yaml`
- `scripts/run-once.sh --mg-id <mg> --subscription-ids <id> [--mode ops|finops|all]`: local dry run; findings land in `./out/findings/*.jsonl`
- `scripts/build-image.sh --param-file deploy.env [--deploy]` (or `make image`): manual image build + ACR push
- `scripts/vm-install.sh --param-file vm.env [--release-ref <sha>]` (or `make vm-install`): install/update on a RHEL 9 VM via Ansible

## Hard rules

- **Config over code.** Thresholds, tag names, MG IDs, and LAW/DCR IDs come from `config/` or app
  settings.
- **Permissions come from IAM.** The external IAM repo owns the UAMI and its role assignments. Add every new
  permission to `identity/role-requirements.yaml` first ([identity](docs/agents/identity.md)); if a change
  seems to need an identity resource, explain why in the PR instead.
- **Managed identity only.** Secrets are Key Vault references. Keys, SAS tokens, and connection strings stay
  out of settings.
- **Stateless runs.** The app keeps no state of its own; storage holds only Functions host state. Findings
  go to the two LAW tables; their schema lives only in `schema/findings-tables.json`.
- **Thin clients.** SDK calls live only in `inventory/`, `metrics/`, and `storage/`.
- **Pinned APIs.** When an Azure API surprises you, pin its API version and record it in `docs/gotchas.md`.
- **Small PRs.** One resource type or one schema change per PR.

## Detailed instructions

- [Architecture](docs/agents/architecture.md): pipeline, batch-metrics limits, resource types, scope, why not Advisor
- [Thresholds](docs/agents/thresholds.md): threshold config, tag overrides, exclusion, RG ignore list
- [Findings](docs/agents/findings.md): the two LAW tables, schema rules, ownership tags, downstream routing
- [Recommendations](docs/agents/recommendations.md): `recommend/` rules and contract
- [Identity](docs/agents/identity.md): UAMI, the permission workflow, self-check, secrets
- [Deployment](docs/agents/deployment.md): image build/push (CI and manual), parameter contract, `deploy.sh` vs Terraform vs RHEL VM (Ansible), CI
- [Roadmap](docs/agents/roadmap.md): the next resource types, steps for adding one, backlog ADRs

## Agent skills

- Issues: GitHub `vindimy/claude-azure-o11y` via `gh` ([issue-tracker](docs/agents/issue-tracker.md)); triage labels in [triage-labels](docs/agents/triage-labels.md)
- Domain docs: `CONTEXT.md` + `docs/adr/` ([domain](docs/agents/domain.md))
