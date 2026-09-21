# o11y alerting: RHEL 9 VM package

Everything needed to install or update the o11y alerting runner on a RHEL 9 VM that cannot reach the
source repository, GitHub, or GitLab. The VM reaches only RHUI (`dnf`), a PyPI index (`pip`), and Azure.

```
RELEASE                     release id, package version, source commit, build time
release.tar.gz              src/ config/ identity/ requirements*.txt, the same bundle vm-install.sh ships
ansible/                    the o11y_alerting role and playbook, unchanged
install.sh                  the installer; runs the playbook against this VM
package.env.example         the parameters install.sh reads
requirements-installer.txt  ansible-core pin for the installer's own venv
lib/params.sh               parameter-file parser shared with the repo's scripts
```

Prerequisites: the VM runs RHEL 9 with the user-assigned managed identity attached, and the findings
tables, DCE, and DCR already exist in Azure. Fill in `package.env` (or have it written by
`scripts/vm-package-env.sh` on a workstation with `az`), then:

```bash
chmod 600 package.env
sudo ./install.sh --param-file package.env
```

Re-running with the same package reconciles the settings file and timers. To update, extract a newer
package and run its `install.sh`; to roll back, run the older package's `install.sh` again. The full
runbook is `docs/ops/azure-vm.md` in the source repository.
