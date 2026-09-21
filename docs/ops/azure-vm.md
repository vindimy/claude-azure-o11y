# Azure Compute VM (RHEL 9)

Path C runs the same `src/` pipeline on a RHEL 9 virtual machine from systemd timers instead of a
Function App. There is no container and no Functions host. `scripts/vm-install.sh` runs on your
workstation and drives `ansible/playbook.yml` over SSH; it is both the installer and the updater.

## Prerequisites

### The VM

Provisioning the VM is outside this repo. Before the first install it must have:

- RHEL 9 (the role asserts this) and outbound access to RHUI for `dnf`, a PyPI index
  (the approved mirror, via `pip_index_url`), Azure management and ingestion endpoints, and
  `prices.azure.com`.
- The UAMI (`UAMI_RESOURCE_ID`) **attached** as a user-assigned identity. The role checks that the
  Instance Metadata Service issues a token for it and stops if not.
- An admin user reachable over SSH from your workstation with passwordless sudo.

Attach the identity if it is not:

```bash
az vm identity assign -g rg-o11y-vm -n vm-o11y-01 --identities "$UAMI_RESOURCE_ID"
```

The UAMI needs every row of `identity/role-requirements.yaml` except `host_storage` and `acr_pull`,
which only a Function App uses. `findings_ingest` is granted after the first install, as for every
style ([order of operations](README.md#first-time-order-of-operations)).

### Other Azure resources

Existing resource group (receives the findings DCE and DCR), Log Analytics workspace, and optionally an
App Insights component in the same RG. The UAMI may be in any RG or subscription. No storage account,
Key Vault, or ACR.

### Your workstation

`az` logged in with rights to read the UAMI, write the LAW tables, and create the DCE/DCR in the RG;
`ansible-core` on the path (`.venv/bin/pip install ansible-core`, then activate the venv or add
`.venv/bin` to `PATH`); an SSH key for the VM's admin user; a clean checkout of the commit to install.

## Install

```bash
cp scripts/vm.env.example vm.env
$EDITOR vm.env              # RESOURCE_GROUP_NAME UAMI_RESOURCE_ID MANAGEMENT_GROUP_ID LAW_RESOURCE_ID VM_HOST VM_SSH_USER
scripts/vm-install.sh --param-file vm.env
```

A `deploy.env` from the Function App path also works; the Function-App-only keys are ignored. Flags
override the file, and anything after `--` goes to `ansible-playbook`:

```bash
scripts/vm-install.sh --param-file vm.env -- --private-key ~/.ssh/id_vm
scripts/vm-install.sh --param-file vm.env --inventory ansible/inventory.ini   # several VMs in an o11y_vm group
scripts/vm-install.sh --param-file vm.env --dry-run true                      # findings to files on the VM
scripts/vm-install.sh --param-file vm.env --no-findings-infra                 # tables/DCE/DCR owned by deploy.sh or Terraform
make vm-install PARAMS=vm.env                                                  # same as the first form
```

What the script does on the workstation: packs `src/ config/ identity/ requirements*.txt` from the
commit with `git archive` (release id = full commit SHA, the VM's equivalent of `image_tag`), resolves
the UAMI client ID, creates or updates the findings tables, DCE, and DCR, resolves the App Insights
connection string, writes the extra vars to a `0600` temp file, and runs the playbook.

What the role does on the VM: asserts RHEL 9 and a working IMDS token, installs `python3.11`, creates the
`o11y` system user, unpacks the release into `/opt/o11y-alerting/releases/<sha>/` with its own venv,
smoke-tests the import and this MG's threshold config, writes `/etc/o11y-alerting/o11y-alerting.env`,
switches the `current` symlink, installs the units, validates the converted schedules with
`systemd-analyze calendar`, enables both timers, and keeps the three newest releases. A release that
fails the smoke test never becomes `current`.

The run ends by printing the findings DCR ID (for the IAM repo) and `systemctl list-timers` output.

### Layout on the VM

| Path | What |
|------|------|
| `/opt/o11y-alerting/current` | symlink to the live release |
| `/opt/o11y-alerting/releases/<sha>/` | code, `config/`, `identity/`, `venv/` |
| `/etc/o11y-alerting/o11y-alerting.env` | runtime settings, `root:o11y 0640`; the App Insights string lives here |
| `/etc/systemd/system/o11y-alerting@.service` | one run; `%i` is `ops` or `finops` |
| `/etc/systemd/system/o11y-alerting-{ops,finops}.timer` | the schedules, converted from NCRONTAB to `OnCalendar` (UTC) |
| `/var/lib/o11y-alerting/out/findings/*.jsonl` | dry-run output |

## Operate

All commands below run on the VM with sudo.

### Check it is running

```bash
sudo systemctl list-timers 'o11y-alerting-*'           # NEXT / LAST per mode
sudo systemctl status o11y-alerting@ops.service         # last ops run, exit status
readlink /opt/o11y-alerting/current                     # which release is live
grep O11Y_RELEASE /etc/o11y-alerting/o11y-alerting.env
```

### Trigger a run now

```bash
sudo systemctl start o11y-alerting@ops.service          # blocks until the run finishes
sudo systemctl start o11y-alerting@finops.service
```

The service is `Type=oneshot` with `TimeoutStartSec=1800`, the same 30-minute limit as
`functionTimeout`. A timer will not start a second copy while one is running.

For a one-off dry run that does not touch the workspace or the live settings:

```bash
cd /opt/o11y-alerting/current
sudo -u o11y bash -c 'set -a; . /etc/o11y-alerting/o11y-alerting.env; set +a;
  DRY_RUN=true OUTPUT_DIR=/tmp/o11y-out RUN_MODES=ops venv/bin/python src/run_local.py'
```

### Read the logs

JSON lines in journald, and in App Insights `traces` when `app_insights_name` was set (the app exports
them itself through OpenTelemetry on the VM):

```bash
sudo journalctl -u 'o11y-alerting@*' -f                                   # follow
sudo journalctl -u o11y-alerting@finops.service --since today             # one mode
sudo journalctl -u 'o11y-alerting@*' --since -1d -o cat | grep '"run complete"' | jq .
sudo journalctl -u 'o11y-alerting@*' --since -1d -o cat | grep -E '"level": "(ERROR|WARNING)"' | jq -c '{ts,msg,resource_id,need}'
```

Then confirm rows with the query in [README.md](README.md#confirming-the-function-works).

### Update

Re-run the installer with the commit to install. `HEAD` needs a clean tree; `--release-ref` can be any
commit, tag, or branch:

```bash
git pull
scripts/vm-install.sh --param-file vm.env --release-ref 4b46694
scripts/vm-install.sh --param-file vm.env --allow-dirty          # tracked, uncommitted changes as dev-<sha>-<ts>
```

The new release gets its own venv, is smoke-tested, then becomes `current`. The next timer fires the
new code; a run already in progress finishes on the old release directory. Re-running with the same
release only reconciles the settings file and units.

### Rollback

```bash
scripts/vm-install.sh --param-file vm.env --release-ref <previous sha>
```

The three newest releases stay on disk, so rolling back to one of them skips the pip install. Anything
older is fetched from git and rebuilt.

### Change settings

Schedules, scope, dry run, and App Insights come from `vm.env`. Change the file and re-run the installer
with the same release; it rewrites the env file and timers, and reloads systemd. Editing
`/etc/o11y-alerting/o11y-alerting.env` by hand works until the next install overwrites it. Timers must
be re-templated, so a schedule change always goes through the installer.

A schedule that restricts both day-of-month and day-of-week is rejected: cron ORs those fields,
`OnCalendar` ANDs them.

### Pause and resume

```bash
sudo systemctl stop o11y-alerting-ops.timer o11y-alerting-finops.timer       # until next boot or start
sudo systemctl disable --now o11y-alerting-finops.timer                      # one mode, survives reboot
sudo systemctl enable --now o11y-alerting-finops.timer
```

Missed runs are not caught up (`Persistent=false`), the same as the Function App. The next installer run
re-enables both timers.

### Verify the identity

The installer runs `scripts/check-identity.sh` before the playbook and prints `ok`, `MISSING`, or `skip`
per row of `identity/role-requirements.yaml`. `host_storage` and `acr_pull` are skipped as not needed
on a VM; `secrets` is skipped because the VM path has no Key Vault. A `MISSING` row never stops the
install (`--skip-identity-check` turns the check off). To run it by hand:

```bash
scripts/check-identity.sh --uami-resource-id "$UAMI_RESOURCE_ID" \
  --management-group-id mg-prod --law-resource-id "$LAW_RESOURCE_ID" \
  --findings-dcr-id "$(az group show -n rg-o11y-test --query id -o tsv)/providers/Microsoft.Insights/dataCollectionRules/dcr-o11y-findings" \
  --skip host_storage,acr_pull
```

From the VM, repeat the token request the role makes:

```bash
CLIENT_ID=$(grep AZURE_CLIENT_ID /etc/o11y-alerting/o11y-alerting.env | cut -d'"' -f2)
curl -s -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/&client_id=$CLIENT_ID" | jq -r .access_token | cut -c1-20
```

### Remove

There is no uninstall script. On the VM:

```bash
sudo systemctl disable --now o11y-alerting-ops.timer o11y-alerting-finops.timer
sudo rm -f /etc/systemd/system/o11y-alerting@.service /etc/systemd/system/o11y-alerting-*.timer
sudo systemctl daemon-reload
sudo rm -rf /opt/o11y-alerting /etc/o11y-alerting /var/lib/o11y-alerting
sudo userdel o11y
```

The findings tables, DCE, and DCR belong to `scripts/destroy.sh` or Terraform
([azure-function.md](azure-function.md#remove)). Detach the UAMI with `az vm identity remove` if the
VM is being repurposed.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `expected RHEL 9, got …` | wrong image | use a RHEL 9 VM |
| `Check the UAMI can get a token from IMDS` fails | UAMI not attached to the VM, or wrong `UAMI_RESOURCE_ID` | `az vm identity assign`; check `vm.env` |
| `logs_ingestion_endpoint and findings_dcr_immutable_id are required when dry_run is false` | `--no-findings-infra` but no DCE/DCR in the RG yet | drop the flag, or deploy the findings path first |
| `working tree has uncommitted changes` | installing `HEAD` from a dirty tree | commit, use `--release-ref`, or `--allow-dirty` |
| pip install fails on the VM | no route to PyPI / mirror | set `PIP_INDEX_URL` to the approved mirror; check egress |
| smoke test fails, `current` unchanged | broken release or invalid `config/` for this MG | fix and re-run; the previous release is still live |
| `systemd-analyze calendar` fails | NCRONTAB with both day-of-month and day-of-week restricted, or a form the converter does not support | change the expression |
| `missing Monitoring Metrics Publisher on data_collection_rule …` in the journal | `findings_ingest` not granted, or granted under 30 minutes ago | grant on the printed DCR ID; wait |
| `ansible-playbook not found` | not on `PATH` | `.venv/bin/pip install ansible-core` and activate the venv |
| SSH or sudo prompts | key or sudoers | `-- --private-key …`, `-- --ask-become-pass` |
| `Job for o11y-alerting@ops.service failed` after 30 min | run exceeded `TimeoutStartSec` | look for a stalled scope; raise `o11y_timeout_sec` only with a reason |
