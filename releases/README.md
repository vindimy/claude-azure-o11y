# Built packages

Self-contained RHEL 9 VM packages, one file per version, built by `scripts/build-vm-package.sh`
(`make vm-package VERSION=<n>`) and installed on the VM with the `install.sh` inside. The `.sha256` next
to each package verifies a download (`sha256sum -c`); the `RELEASE` file inside records the version,
source commit, and build time. A version is built once and never rebuilt in place: bump the version.

How to use them: [docs/ops/azure-vm.md](../docs/ops/azure-vm.md#install-from-a-self-contained-package).
