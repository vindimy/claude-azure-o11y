# CLAUDE.md: Azure Resource Utilization Alerting (Ops + FinOps)

A scheduled Python Azure Function (custom container, v2 model). For every subscription under a management
group, it reads built-in Azure Monitor metrics and produces:

- **Ops alerts** for hot resources (Teams + email), and
- **FinOps findings** for cold resources: a downsizing recommendation with an estimated saving, written to a
  per-type Markdown report.

MVP is VM CPU only. Owner: Dmitriy (Cloud Engineering).

## Commands

- `make test`: pytest; fails below 80% coverage on `evaluate/` and `recommend/`
- `make lint`: ruff + mypy
- `make identity-doc`: regenerate `docs/identity-requirements.md` from `identity/role-requirements.yaml`
- `scripts/run-once.sh --mg-id <mg> --subscription-ids <id>`: local dry run; the report lands in `./out/`

## Hard rules

- **Config over code.** Thresholds, tag names, webhooks, MG IDs, and LAW IDs come from `config/` or app
  settings.
- **Permissions come from IAM.** The external IAM repo owns the UAMI and its role assignments. Add every new
  permission to `identity/role-requirements.yaml` first ([identity](docs/agents/identity.md)); if a change
  seems to need an identity resource, explain why in the PR instead.
- **Managed identity only.** Secrets are Key Vault references. Keys, SAS tokens, and connection strings stay
  out of settings.
- **Stateless runs.** Storage holds only the `reports` and `suppression` containers, plus Functions host
  state.
- **Thin clients.** SDK calls live only in `inventory/`, `metrics/`, and `storage/`.
- **Pinned APIs.** When an Azure API surprises you, pin its API version and record it in `docs/gotchas.md`.
- **Small PRs.** One resource type or one notifier per PR.

## Detailed instructions

- [Architecture](docs/agents/architecture.md): pipeline, batch-metrics limits, resource types, scope, why not Advisor
- [Thresholds](docs/agents/thresholds.md): threshold config, tag overrides, exclusion, RG ignore list, suppression
- [Notifications](docs/agents/notifications.md): ownership tags, routing, Teams/email, FinOps report format
- [Recommendations](docs/agents/recommendations.md): `recommend/` rules and contract
- [Identity](docs/agents/identity.md): UAMI, the permission workflow, self-check, secrets
- [Deployment](docs/agents/deployment.md): image, parameter contract, `deploy.sh` vs Terraform, CI
- [Roadmap](docs/agents/roadmap.md): the next resource types, steps for adding one, backlog ADRs

## Agent skills

- Issues: GitHub `vindimy/claude-azure-o11y` via `gh` ([issue-tracker](docs/agents/issue-tracker.md)); triage labels in [triage-labels](docs/agents/triage-labels.md)
- Domain docs: `CONTEXT.md` + `docs/adr/` ([domain](docs/agents/domain.md))
