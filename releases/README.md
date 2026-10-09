# Built packages

A release `vN` is up to two packages, each built once from a commit and never rebuilt in place (bump the
version instead). The `.sha256` next to each verifies a download (`sha256sum -c`); the `RELEASE` file
inside records the version, source commit, and build time.

| Package | Built by | Installed | Contents |
|---|---|---|---|
| `o11y-alerting-vm-vN.tar.gz` | `scripts/build-vm-package.sh` (`make vm-package VERSION=N`) | on the RHEL 9 VM, `sudo ./install.sh` ([runbook](../docs/ops/azure-vm.md#install-from-a-self-contained-package)) | the pipeline (`src/`, `config/`), the Ansible role, the installer |
| `o11y-insights-vN.tar.gz` | `scripts/build-insights-package.sh` (`make insights-package VERSION=N`) | from a workstation with Terraform, `./install.sh --state-dir DIR` ([runbook](../docs/ops/insights.md#from-a-package-no-repository-needed)) | Ops/FinOps workbooks, log search alerts, action groups, Teams Logic Apps (`terraform/module-o11y-insights`) |

The two halves are independent: either can be installed, updated, or rolled back without the other.
Install the VM package (or any deployment style) first, because the alerts need the findings tables.

| Release | VM package | Insights package | Notes |
|---|---|---|---|
| v1, v2, v3 | yes | — | before the insights module |
| v4, v5 | — | — | skipped; never built |
| v6 | yes | yes | first release with dashboards, alerts, and Teams delivery |

Each release is also published at <https://github.com/vindimy/claude-azure-o11y/releases> under the
tag `vN`, which points at the `main` commit that adds the packages. The packages and their `.sha256`
files are attached as assets. After the packages are committed and pushed:

```bash
git tag -a vN <commit> -m "Release vN: o11y-alerting-vm-vN (<sha>) + o11y-insights-vN (<sha>)"
git push origin vN
gh release create vN --verify-tag --latest --title "vN: <summary>" --notes-file notes.md \
  releases/o11y-alerting-vm-vN.tar.gz{,.sha256} releases/o11y-insights-vN.tar.gz{,.sha256}
```

v1–v3 exist only as files in this directory and have no GitHub release.
