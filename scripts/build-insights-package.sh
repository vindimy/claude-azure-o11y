#!/usr/bin/env bash
# Build the o11y insights package (docs/ops/insights.md, "Install from a package"): the Terraform module
# for the Ops/FinOps workbooks, log search alerts, and Teams delivery, the root that applies it, the runbook,
# and install.sh, which keeps the Terraform state outside the versioned directory. It is the Azure half of a
# release; the RHEL VM half is scripts/build-vm-package.sh, built with the same --version.
# Output: <out-dir>/o11y-insights-v<N>.tar.gz and a .sha256 file next to it.
#
# Usage: build-insights-package.sh --version N [--ref <commit|tag|branch>] [--allow-dirty] [--out-dir DIR] [--force]
#
#   --version      package version (an integer); use the release number of the matching VM package
#   --ref          commit to build from (default HEAD, which needs a clean tree)
#   --allow-dirty  build from the working tree instead: tracked and untracked files, .gitignore honoured;
#                  SOURCE_COMMIT in the RELEASE file gets a -dirty suffix
#   --out-dir      default releases/
#   --force        overwrite an existing package file (a version is meant to be built once)
#
# The last line of output is PACKAGE=<path>.
set -euo pipefail

VERSION=""; REF=""; ALLOW_DIRTY=false; OUT_DIR=releases; FORCE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=true; shift ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --force) FORCE=true; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*" >&2; }
[[ "$VERSION" =~ ^[0-9]+$ ]] || fail "--version must be an integer, e.g. --version 1"
[[ -z "$REF" || "$ALLOW_DIRTY" == false ]] || fail "--ref and --allow-dirty are exclusive"

mkdir -p "$OUT_DIR"; OUT_DIR=$(cd "$OUT_DIR" && pwd)
cd "$(dirname "$0")/.."
NAME=o11y-insights-v$VERSION
OUT=$OUT_DIR/$NAME.tar.gz
[[ ! -e "$OUT" || "$FORCE" == true ]] || fail "$OUT exists; bump --version, or pass --force to rebuild it"

# The module reads only files under its own directory (path.module), so these are self-contained.
CONTENT=(terraform/module-o11y-insights terraform/examples/insights docs/ops/insights.md
         docs/agents/insights.md packaging/insights)

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
STAGE=$TMP/stage; mkdir -p "$STAGE"
export COPYFILE_DISABLE=1   # no AppleDouble ._* entries from macOS tar
if [[ "$ALLOW_DIRTY" == true ]]; then
  SOURCE_COMMIT="$(git rev-parse HEAD)-dirty"; EPOCH=$(date -u +%s)
  log "Packing the working tree ($SOURCE_COMMIT)"
  git ls-files -co --exclude-standard -- "${CONTENT[@]}" \
    | while IFS= read -r f; do [[ -f "$f" ]] && printf '%s\n' "$f"; done >"$TMP/files"
  tar -cf - -T "$TMP/files" | tar -xf - -C "$STAGE"
else
  [[ -n "$REF" || -z "$(git status --porcelain)" ]] \
    || fail "working tree has uncommitted changes; commit, pass --ref, or --allow-dirty"
  SOURCE_COMMIT=$(git rev-parse --verify "${REF:-HEAD}^{commit}") || fail "unknown --ref $REF"
  EPOCH=$(git show -s --format=%ct "$SOURCE_COMMIT")
  log "Packing commit $SOURCE_COMMIT"
  git archive --format=tar "$SOURCE_COMMIT" -- "${CONTENT[@]}" | tar -xf - -C "$STAGE"
fi
for p in "${CONTENT[@]}"; do
  [[ -e "$STAGE/$p" ]] || fail "$p is missing from the source (${REF:-HEAD}); the package needs it"
done

PKG=$TMP/$NAME; mkdir -p "$PKG/docs/ops" "$PKG/docs/agents"
mv "$STAGE/terraform" "$PKG/terraform"
# Local working files never belong in a package, even with --allow-dirty.
find "$PKG/terraform" \( -name .terraform -o -name '.terraform.lock.hcl' -o -name '*.tfstate*' \
  -o -name '*.tfvars' -o -name '*.tfvars.json' -o -name 'tfplan' \) -prune -exec rm -rf {} +
mv "$STAGE/docs/ops/insights.md" "$PKG/docs/ops/insights.md"
mv "$STAGE/docs/agents/insights.md" "$PKG/docs/agents/insights.md"
mv "$STAGE"/packaging/insights/* "$PKG/"
printf 'RELEASE_ID=v%s\nPACKAGE_VERSION=%s\nSOURCE_COMMIT=%s\nBUILT_AT=%s\n' \
  "$VERSION" "$VERSION" "$SOURCE_COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$PKG/RELEASE"
chmod 755 "$PKG/install.sh"

# Neutral metadata (uid/gid 0, mtime = source time), as in build-vm-package.sh.
log "Writing $OUT"
python3 - "$EPOCH" "$TMP" "$NAME" "$OUT" <<'PY'
import gzip, hashlib, os, sys, tarfile
epoch, root, name, out = sys.argv[1:]
epoch = int(epoch)

def norm(ti):
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    ti.mtime = epoch
    if ti.isdir():
        ti.mode = 0o755
    elif ti.isfile():
        ti.mode = 0o755 if ti.mode & 0o100 else 0o644
    return ti

top = os.path.join(root, name)
paths = [top]
for d, dirs, files in os.walk(top):
    dirs[:] = sorted(dirs)
    paths += [os.path.join(d, x) for x in dirs + sorted(files)]
with gzip.GzipFile(out, "wb", mtime=epoch) as gz, tarfile.open(fileobj=gz, mode="w") as tf:
    for p in sorted(set(paths)):
        tf.add(p, arcname=os.path.relpath(p, root), recursive=False, filter=norm)
digest = hashlib.sha256(open(out, "rb").read()).hexdigest()
with open(out + ".sha256", "w") as f:
    f.write(f"{digest}  {os.path.basename(out)}\n")
PY

echo "PACKAGE=$OUT"
