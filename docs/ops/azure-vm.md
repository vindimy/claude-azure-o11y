# Azure Compute VM (RHEL 9)

Path C runs the same `src/` pipeline on a RHEL 9 virtual machine from systemd timers instead of a
Function App. There is no container and no Functions host. `scripts/vm-install.sh` runs on your
workstation and drives `ansible/playbook.yml` over SSH; it is both the installer and the updater.
A [self-contained package](#install-from-a-self-contained-package) is the alternative when the VM
cannot reach this repository, GitHub, or GitLab.

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

Over SSH from your workstation. For a VM that cannot reach this repository, GitHub, or GitLab, use the
[self-contained package](#install-from-a-self-contained-package) instead.

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

## Install from a self-contained package

Use this when the VM, or whoever installs on it, has no access to this repository, GitHub, or GitLab.
A package holds the release bundle, the Ansible role, and a local installer, so the VM reaches only
RHUI (`dnf`), a PyPI index (`pip`), and Azure. Built packages live in [releases/](../../releases/); v1
is `releases/o11y-alerting-vm-v1.tar.gz` with its `.sha256`. The result on the VM is the same layout,
units, and timers as the SSH install, with `v<N>` as the release id instead of a commit SHA.

### Prerequisites

The VM as in [The VM](#the-vm): RHEL 9, the UAMI attached, and outbound access to RHUI, a PyPI index,
Azure management and ingestion endpoints, and `prices.azure.com`. In addition:

- **A sudo-capable user on the VM** to run the installer. No SSH from a workstation and no Ansible on
  a workstation. `install.sh` pip-installs a pinned `ansible-core` from the PyPI index into its own
  venv; `--ansible-playbook /usr/bin/ansible-playbook` uses one from `dnf install ansible-core` instead.
- **The findings tables, DCE, and DCR already in the resource group.** Anything that deploys the
  findings path creates them: `deploy.sh`, Terraform, `vm-install.sh`, or `scripts/vm-package-env.sh`
  below. The installer cannot create them: the VM has no `az`, and the UAMI has no rights to.
- **The UAMI roles** as above; `findings_ingest` on the DCR after the findings path exists.
- **A way to copy two files to the VM:** the package and a `package.env`. `scp`, a storage account, or
  an internal artifact store all work.

`package.env` holds the contract parameters plus the values `vm-install.sh` would have resolved with `az`:

| Key | Where it comes from |
|-----|---------------------|
| `MANAGEMENT_GROUP_ID`, `SUBSCRIPTION_IDS`, `LAW_RESOURCE_ID`, `OPS_SCHEDULE_CRON`, `FINOPS_SCHEDULE_CRON`, `DRY_RUN`, `PIP_INDEX_URL` | same meaning as in `vm.env` |
| `UAMI_RESOURCE_ID` | the attached UAMI; the installer resolves its client ID through IMDS. `UAMI_CLIENT_ID` instead skips that lookup |
| `LOGS_INGESTION_ENDPOINT`, `FINDINGS_DCR_IMMUTABLE_ID` | the findings DCE and DCR; required unless `DRY_RUN=true` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | optional; keep the file `0600` when set |

### Build a package (workstation, once per version)

```bash
make vm-package VERSION=2                                  # releases/o11y-alerting-vm-v2.tar.gz + .sha256, from a clean HEAD
scripts/build-vm-package.sh --version 2 --ref <sha|tag>    # from another commit
scripts/build-vm-package.sh --version 2 --allow-dirty      # from the working tree; SOURCE_COMMIT gets -dirty
```

A version is built once: the script refuses to overwrite an existing file without `--force`. Commit
both files under `releases/`. The `RELEASE` file inside records the version, source commit, and build
time; it is also unpacked into the release directory on the VM.

### Write package.env (workstation with az)

```bash
(umask 077; scripts/vm-package-env.sh --param-file vm.env > package.env)
scripts/vm-package-env.sh --param-file vm.env --no-findings-infra > package.env   # DCE/DCR owned by Terraform or deploy.sh
```

Like `vm-install.sh`, it creates or updates the findings tables, DCE, and DCR, runs the identity
check, and prints the DCR ID for the IAM repo; only the file goes to stdout. Without a checkout of
this repo, start from the `package.env.example` inside the package and look the values up:

```bash
az identity show --ids "$UAMI_RESOURCE_ID" --query clientId -o tsv                 # UAMI_CLIENT_ID (optional)
RG_ID=$(az group show -n rg-o11y-test --query id -o tsv)
az rest --method get --query properties.logsIngestion.endpoint -o tsv \
  --url "https://management.azure.com$RG_ID/providers/Microsoft.Insights/dataCollectionEndpoints/dce-o11y-findings?api-version=2023-03-11"
az rest --method get --query properties.immutableId -o tsv \
  --url "https://management.azure.com$RG_ID/providers/Microsoft.Insights/dataCollectionRules/dcr-o11y-findings?api-version=2023-03-11"
```

### Install (on the VM)

```bash
sha256sum -c o11y-alerting-vm-v1.tar.gz.sha256
tar xzf o11y-alerting-vm-v1.tar.gz && cd o11y-alerting-vm-v1
chmod 600 ../package.env
sudo ./install.sh --param-file ../package.env
sudo ./install.sh --param-file ../package.env --dry-run true      # flags override the file
sudo ./install.sh --param-file ../package.env -- -v               # extra ansible-playbook args
```

What `install.sh` does: validates the parameters and RHEL 9, `dnf` installs `python3.11`, pip-installs
the pinned `ansible-core` into `.installer-venv` inside the package directory (through `PIP_INDEX_URL`
when set), asks IMDS for a token for `UAMI_RESOURCE_ID` and reads the client ID from it (which also
proves the identity is attached), writes the extra vars to a `0600` temp file, and runs the packaged
`ansible/playbook.yml` against `localhost`. From there the role does exactly what it does over SSH
([above](#install)). `/opt/o11y-alerting/releases/v1/RELEASE` records what was installed.

### Update, rollback, and settings with packages

- **Update:** extract the newer package and run its `install.sh`. The new release gets its own venv
  and smoke test before `current` moves.
- **Rollback:** run `install.sh` from the older package directory again (keep the extracted
  directories, or re-extract). A release still among the three kept on disk skips the pip install.
- **Settings:** edit `package.env` and re-run `install.sh` with the same package; it rewrites the env
  file and timers, as in [Change settings](#change-settings).
- **Same version, different build:** if `releases/v<N>` on the VM came from another `SOURCE_COMMIT`,
  the installer stops. Build a new version, or pass `--reinstall` to replace it.
- **Remove:** as in [Remove](#remove), then delete the extracted package directories.

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
| `is not a complete package directory` | `install.sh` run from outside the extracted package | `cd o11y-alerting-vm-v<N>` and run `./install.sh` there |
| `IMDS token request failed` (package install) | UAMI not attached, or wrong `UAMI_RESOURCE_ID` in `package.env` | `az vm identity assign`; check the file |
| `release v<N> is already installed from another build` | the version was rebuilt from a different commit | build a new version, or `--reinstall` |
| pip install of `ansible-core` fails (package install) | no route to PyPI / mirror | set `PIP_INDEX_URL`, or `dnf install ansible-core` and `--ansible-playbook /usr/bin/ansible-playbook` |
| SSH or sudo prompts | key or sudoers | `-- --private-key …`, `-- --ask-become-pass` |
| `Job for o11y-alerting@ops.service failed` after 30 min | run exceeded `TimeoutStartSec` | look for a stalled scope; raise `o11y_timeout_sec` only with a reason |
