#!/usr/bin/env bash
# Build the self-contained RHEL 9 VM package (docs/ops/azure-vm.md, "Install from a self-contained
# package"): the release bundle, the Ansible role and a local installer, so the VM needs no git, GitHub or
# GitLab access. Output: <out-dir>/o11y-alerting-vm-v<N>.tar.gz and a .sha256 file next to it.
#
# Usage: build-vm-package.sh --version N [--ref <commit|tag|branch>] [--allow-dirty] [--out-dir DIR] [--force]
#
#   --version      package version (an integer); the release id on the VM is v<N>
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
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*" >&2; }
[[ "$VERSION" =~ ^[0-9]+$ ]] || fail "--version must be an integer, e.g. --version 1"
[[ -z "$REF" || "$ALLOW_DIRTY" == false ]] || fail "--ref and --allow-dirty are exclusive"

# Resolve the output dir before moving to the repo root, so relative paths keep working.
mkdir -p "$OUT_DIR"; OUT_DIR=$(cd "$OUT_DIR" && pwd)
cd "$(dirname "$0")/.."
NAME=o11y-alerting-vm-v$VERSION
OUT=$OUT_DIR/$NAME.tar.gz
[[ ! -e "$OUT" || "$FORCE" == true ]] || fail "$OUT exists; bump --version, or pass --force to rebuild it"

# What the app needs at runtime (the same list vm-install.sh sends), and what the installer needs.
PAYLOAD=(src config identity requirements.txt requirements-vm.txt)
PACKAGE=(ansible packaging/vm scripts/lib/params.sh)

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
STAGE=$TMP/stage; mkdir -p "$STAGE"
export COPYFILE_DISABLE=1   # no AppleDouble ._* entries from macOS tar
if [[ "$ALLOW_DIRTY" == true ]]; then
  SOURCE_COMMIT="$(git rev-parse HEAD)-dirty"; EPOCH=$(date -u +%s)
  log "Packing the working tree ($SOURCE_COMMIT)"
  git ls-files -co --exclude-standard -- "${PAYLOAD[@]}" "${PACKAGE[@]}" \
    | while IFS= read -r f; do [[ -f "$f" ]] && printf '%s\n' "$f"; done >"$TMP/files"
  tar -cf - -T "$TMP/files" | tar -xf - -C "$STAGE"
else
  [[ -n "$REF" || -z "$(git status --porcelain)" ]] \
    || fail "working tree has uncommitted changes; commit, pass --ref, or --allow-dirty"
  SOURCE_COMMIT=$(git rev-parse --verify "${REF:-HEAD}^{commit}") || fail "unknown --ref $REF"
  EPOCH=$(git show -s --format=%ct "$SOURCE_COMMIT")
  log "Packing commit $SOURCE_COMMIT"
  git archive --format=tar "$SOURCE_COMMIT" -- "${PAYLOAD[@]}" "${PACKAGE[@]}" | tar -xf - -C "$STAGE"
fi
for p in "${PAYLOAD[@]}" "${PACKAGE[@]}"; do
  [[ -e "$STAGE/$p" ]] || fail "$p is missing from the source (${REF:-HEAD}); the package needs it"
done

printf 'RELEASE_ID=v%s\nPACKAGE_VERSION=%s\nSOURCE_COMMIT=%s\nBUILT_AT=%s\n' \
  "$VERSION" "$VERSION" "$SOURCE_COMMIT" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$STAGE/RELEASE"

PKG=$TMP/$NAME; mkdir -p "$PKG/lib"
mv "$STAGE/ansible" "$PKG/ansible"
mv "$STAGE/scripts/lib/params.sh" "$PKG/lib/params.sh"
mv "$STAGE"/packaging/vm/* "$PKG/"
cp "$STAGE/RELEASE" "$PKG/RELEASE"
chmod 755 "$PKG/install.sh"

# Both tarballs get neutral metadata (uid/gid 0, mtime = source time), so the file does not carry the
# builder's account and rebuilding the same commit gives the same bytes apart from BUILT_AT.
log "Writing $OUT"
python3 - "$EPOCH" "$STAGE" "$PKG/release.tar.gz" "$TMP" "$NAME" "$OUT" "${PAYLOAD[@]}" RELEASE <<'PY'
import gzip, hashlib, os, sys, tarfile
epoch, stage, release_out, pkg_root, pkg_name, out, *payload = sys.argv[1:]
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

def pack(root, members, path):
    with gzip.GzipFile(path, "wb", mtime=epoch) as gz, tarfile.open(fileobj=gz, mode="w") as tf:
        for m in members:
            top = os.path.join(root, m)
            paths = [top]
            for d, dirs, files in os.walk(top):
                dirs[:] = sorted(x for x in dirs if x != "__pycache__")
                paths += [os.path.join(d, x) for x in dirs + sorted(files)]
            for p in sorted(set(paths)):
                tf.add(p, arcname=os.path.relpath(p, root), recursive=False, filter=norm)

pack(stage, payload, release_out)
pack(pkg_root, [pkg_name], out)
digest = hashlib.sha256(open(out, "rb").read()).hexdigest()
with open(out + ".sha256", "w") as f:
    f.write(f"{digest}  {os.path.basename(out)}\n")
PY

echo "PACKAGE=$OUT"
