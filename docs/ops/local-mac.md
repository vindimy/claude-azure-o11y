# Local Mac environment

Run the pipeline from a checkout on macOS with your own `az login` credentials. This is how you develop,
test threshold changes against real metrics, and do the occasional live run without waiting for a
schedule. The same workstation is also where the Function App and VM deploys are driven from, so the
tooling section covers those too.

## Install

### Tools

| Tool | Needed for | Install |
|------|-----------|---------|
| Python 3.11+ | everything | `brew install python@3.11` |
| Azure CLI, logged in | everything | `brew install azure-cli && az login` |
| Docker Desktop | building the image locally (`build-image.sh --builder docker`) | docker.com; or use `--builder acr` and skip it |
| Terraform | Path B deploys from a workstation (CI normally does this) | `brew install terraform` |
| ansible-core | RHEL VM installs (Path C) | `.venv/bin/pip install ansible-core` |

Stock `/bin/bash` 3.2 is fine; the scripts are written for it.

### Checkout and virtualenv

```bash
git clone git@github.com:vindimy/claude-azure-o11y.git && cd claude-azure-o11y
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make test && make lint      # confirms the environment before you touch Azure
```

`requirements-dev.txt` includes the runtime requirements. Scripts use `.venv/bin/python` by default;
set `PY=/path/to/python` to override.

### Azure access

Select the subscription the CLI should bill management calls to, then confirm your user can see the
scope you will evaluate:

```bash
az account set --subscription <any subscription you can use>
az account management-group show -n mg-nonprod -o table
```

Your **user** (not the UAMI) needs, on the management group or subscriptions you point at:

- `Reader` (Resource Graph inventory)
- `Monitoring Reader` (Metrics Batch API)
- `Monitoring Metrics Publisher` on the findings DCR, only for live runs (see below)

Retail prices come from the public `prices.azure.com` endpoint with no authentication, so FinOps runs
need outbound HTTPS to it.

## Operate

### Dry run against a real scope

Nothing is written to Log Analytics. Rows land in `./out/findings/<table>.jsonl`:

```bash
scripts/run-once.sh --mg-id mg-nonprod                                    # whole MG, ops then finops
scripts/run-once.sh --mg-id mg-nonprod --subscription-ids "<sub-id>,<sub-id>" --mode ops
scripts/run-once.sh --mg-id mg-nonprod --subscription-ids "<sub-id>" --mode finops --output-dir /tmp/o11y
```

`--mg-id` is required even with `--subscription-ids`: it selects `config/thresholds/<mg-id>.yaml` and is
stamped on every row as `ManagementGroupId`. With `--subscription-ids` set, the subscriptions replace
the MG as the scope (fallback mode).

Output, one line per mode:

```text
ops: findings=3 written=3 skips=12 -> ./out/findings/O11yOpsFindings_CL.jsonl
finops: findings=41 written=41 skips=12 -> ./out/findings/O11yFinOpsFindings_CL.jsonl
```

Logs are JSON lines on stderr. Useful filters:

```bash
scripts/run-once.sh --mg-id mg-nonprod --mode ops 2>&1 | grep '"run complete"' | python3 -m json.tool
scripts/run-once.sh --mg-id mg-nonprod --mode ops 2>&1 | grep '"skipped resource"' | cut -c1-200
LOG_LEVEL=DEBUG scripts/run-once.sh --mg-id mg-nonprod --mode ops
```

Read the results:

```bash
python3 -c 'import json,sys; [print(r["ResourceName"], r["ObservedValue"], r["Threshold"], r["Owner"]) for r in map(json.loads, open("out/findings/O11yOpsFindings_CL.jsonl"))]'
```

`out/` is appended to, not truncated. Delete it between runs if you want a clean file.

### Live run from your Mac

Use this to prove the ingestion path or to backfill one run without waiting for the schedule. It writes
real rows to the workspace, so point it at a non-production MG unless you mean it.

`--live` resolves the ingestion endpoint and DCR immutable ID from the findings DCE and DCR in the
resource group, the same lookup the deploys use, and sets `DRY_RUN=false`. The resource group and MG
can come from a `deploy.env` or `vm.env`, or from flags:

```bash
scripts/run-once.sh --param-file deploy.env --live --mode finops
scripts/run-once.sh --mg-id mg-nonprod --resource-group rg-o11y-test --live --mode ops
scripts/run-once.sh --param-file deploy.env --live --subscription-ids "<sub-id>" --mode ops
```

The script logs `LIVE: writing findings for <mg> to the workspace through <dcr id>` before it starts.
Your user needs `Monitoring Metrics Publisher` on that DCR; a 403 is reported as
`PermissionMissing("findings_ingest")` with the matching row of `identity/role-requirements.yaml`.

Without the script, the same thing by hand is `DRY_RUN=false RUN_MODES=finops
.venv/bin/python src/run_local.py` with `MG_ID`, `LOGS_INGESTION_ENDPOINT`, and
`FINDINGS_DCR_IMMUTABLE_ID` in `.env`. Note that `run_local.py` defaults `DRY_RUN` to `true` before
`.env` is read, so `DRY_RUN` must be on the command line, not in the file.

### Tests, lint, generated docs

```bash
make test           # pytest; coverage gate 80% on evaluate/ and recommend/
make lint           # ruff check, ruff format --check, mypy
make identity-doc   # regenerate docs/identity-requirements.md after editing identity/role-requirements.yaml
```

### Building and pushing the image from a Mac

Only for the Function App styles; the VM path has no image. Details in
[azure-function.md](azure-function.md#build-the-image). Two Mac-specific points:

- The script always builds `linux/amd64`. An arm64 image from Apple Silicon will not start on the
  Functions host.
- `build/base-image.txt` must name the approved base registry; the script refuses to build while the
  file still holds the `<approved-registry>` placeholder.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `--mg-id is required` | `scripts/run-once.sh` needs the MG even in subscription mode | pass `--mg-id` |
| `.venv/bin/python: No such file` | no virtualenv, or a different path | create `.venv` as above or set `PY=` |
| `PermissionMissing("inventory")` / `("metrics")` | your user lacks Reader / Monitoring Reader on the scope | request the role, or pick a subscription you can read |
| `PermissionMissing("findings_ingest")` on a live run | your user lacks Monitoring Metrics Publisher on the DCR, or the grant is under 30 minutes old | request it; wait; retry |
| `--live needs --resource-group` | no RG to resolve the DCE/DCR from | pass `--resource-group` or a param file with `RESOURCE_GROUP_NAME` |
| `findings DCE/DCR not found in RG …` | nothing deployed to that RG yet | deploy once ([azure-function.md](azure-function.md) or [azure-vm.md](azure-vm.md)), or use dry run |
| `findings=0` on a scope that has VMs | thresholds not crossed, `DataCoverage` below `min_coverage`, or everything ignored | check `skips`, `ignored_rg_count`, and `excluded` in the `run complete` line |
| every row has `MissingTags` | resources lack `owner` / `assignment_group` / `car_id`, or `config/assignment-groups.yaml` lacks the group | tag the resources or add the group |
| slow FinOps run | one Retail Prices call per distinct SKU/region pair | expected on first run per region; `MAX_CONCURRENCY` (default 8) bounds the metrics fan-out only |
