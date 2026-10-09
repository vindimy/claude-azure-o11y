# o11y insights package: dashboards, alerts, and Teams delivery

The Azure side of an o11y alerting release: two Azure Workbooks (Ops findings, FinOps recommendations),
log search alerts routed by `AssignmentGroup`, action groups, and the Logic Apps that post Adaptive Cards
to Teams channels through Teams Workflows webhooks. It reads the findings tables the pipeline writes, so
it works with every deployment style (Function App or RHEL VM). Apply it from a workstation; nothing in
here runs on the VM.

```
RELEASE                              release id, package version, source commit, build time
install.sh                           terraform init/plan/apply with the state kept outside this directory
terraform/module-o11y-insights/      the module: queries/*.kql, workbooks/*.json, logicapps/*.json, samples/
terraform/examples/insights/         the root install.sh applies, with terraform.tfvars.example
docs/ops/insights.md                 the runbook (Teams workflows, Key Vault secrets, verify, troubleshoot)
docs/agents/insights.md              how the alerts, routing, and cards work
```

Prerequisites: the pipeline has written rows to both findings tables, and Terraform >= 1.9 and `az login`
are on this workstation. For Teams, create one Teams Workflows webhook per channel and store each URL as a
Key Vault secret (runbook steps 1–2). The Logic Apps' identity needs the `law` and `secrets` rows of
`identity/role-requirements.yaml` (Log Analytics Reader on the workspace, Key Vault Secrets User on the
vault).

```bash
sha256sum -c o11y-insights-v<N>.tar.gz.sha256
tar xzf o11y-insights-v<N>.tar.gz && cd o11y-insights-v<N>
./install.sh --state-dir ~/o11y-insights      # first run: creates ~/o11y-insights/terraform.tfvars, then stops
$EDITOR ~/o11y-insights/terraform.tfvars
./install.sh --state-dir ~/o11y-insights      # init, plan, confirm, apply; prints the workbook links
```

Always pass the same `--state-dir`: it holds the Terraform state and your settings, so a newer package
updates the existing resources. To update, extract the newer package and run its `install.sh` with the
same directory. To roll back, run the older package's `install.sh` the same way. `--plan-only` shows
the changes without applying them, and `--destroy` removes the resources (the findings tables and their data stay).
For a team setup, put the state in a shared backend (for example an `azurerm` backend in
`terraform/examples/insights/versions.tf`) instead of a local directory.
