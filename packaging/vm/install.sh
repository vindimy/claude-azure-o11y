#!/usr/bin/env bash
# Install or update the o11y alerting runner on THIS RHEL 9 VM from the self-contained package built by
# scripts/build-vm-package.sh. It runs the repo's ansible/playbook.yml locally, so the VM never needs git,
# GitHub or GitLab; it reaches only RHUI (dnf) and a PyPI index (pip; PIP_INDEX_URL for a mirror).
#
# Usage: sudo ./install.sh --param-file package.env [--<param> <value> ...] [--ansible-playbook PATH]
#                          [--reinstall] [-- <extra ansible-playbook args>]
#
#   --param-file        package.env (see package.env.example); flags override the file
#   --ansible-playbook  use this ansible-playbook (e.g. /usr/bin/ansible-playbook after
#                       `dnf install ansible-core`) instead of installing ansible-core into ./.installer-venv
#   --reinstall         replace an installed release that has the same id but a different SOURCE_COMMIT
#   -- ...              passed to ansible-playbook, e.g. -- -v
#
# The release id (RELEASE file) becomes /opt/o11y-alerting/releases/<id>; re-running with the same package
# only reconciles the settings file and the timers.
set -euo pipefail
cd "$(dirname "$0")"

PARAMS=(MANAGEMENT_GROUP_ID SUBSCRIPTION_IDS LAW_RESOURCE_ID OPS_SCHEDULE_CRON FINOPS_SCHEDULE_CRON
        DRY_RUN PIP_INDEX_URL UAMI_RESOURCE_ID UAMI_CLIENT_ID LOGS_INGESTION_ENDPOINT
        FINDINGS_DCR_IMMUTABLE_ID APPLICATIONINSIGHTS_CONNECTION_STRING)
REQUIRED=(MANAGEMENT_GROUP_ID LAW_RESOURCE_ID)
INSTALL_DIR=/opt/o11y-alerting   # the role's o11y_install_dir default

usage() {
  sed -n '2,16p' "$0"
  echo "Parameters (flags override the file):"
  for p in "${PARAMS[@]}"; do echo "  --$(echo "$p" | tr '[:upper:]_' '[:lower:]-')"; done
}

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*"; }

[[ -f lib/params.sh && -f RELEASE && -f release.tar.gz && -f ansible/playbook.yml ]] \
  || fail "$PWD is not a complete package directory; extract the o11y-alerting-vm-v<N>.tar.gz and run install.sh from inside it"
# shellcheck source-path=SCRIPTDIR source=../../scripts/lib/params.sh
source lib/params.sh
load_params RELEASE   # RELEASE_ID, PACKAGE_VERSION, SOURCE_COMMIT, BUILT_AT

PARAM_FILE=""; ANSIBLE_PLAYBOOK=""; REINSTALL=false
OVERRIDES=(); ANSIBLE_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --ansible-playbook) ANSIBLE_PLAYBOOK="$2"; shift 2 ;;
    --reinstall) REINSTALL=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; ANSIBLE_ARGS=("$@"); break ;;
    --*) [[ $# -ge 2 ]] || fail "$1 needs a value"; OVERRIDES+=("$(flag_to_var "$1")=$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$PARAM_FILE" ]]; then load_params "$PARAM_FILE"; fi
# shellcheck disable=SC2163  # kv is "KEY=value"
for kv in ${OVERRIDES[@]+"${OVERRIDES[@]}"}; do export "$kv"; done

: "${OPS_SCHEDULE_CRON:=0 */15 * * * *}" "${FINOPS_SCHEDULE_CRON:=0 0 6 * * *}" "${DRY_RUN:=false}"
: "${SUBSCRIPTION_IDS:=}" "${PIP_INDEX_URL:=}" "${UAMI_RESOURCE_ID:=}" "${UAMI_CLIENT_ID:=}"
: "${LOGS_INGESTION_ENDPOINT:=}" "${FINDINGS_DCR_IMMUTABLE_ID:=}" "${APPLICATIONINSIGHTS_CONNECTION_STRING:=}"

for r in "${REQUIRED[@]}"; do
  [[ -n "${!r:-}" ]] || fail "missing required parameter: $r"
done
[[ "$DRY_RUN" == true || "$DRY_RUN" == false ]] || fail "DRY_RUN must be true or false, got $DRY_RUN"
[[ -n "$UAMI_RESOURCE_ID" || -n "$UAMI_CLIENT_ID" ]] \
  || fail "set UAMI_RESOURCE_ID (resolved to its client ID through IMDS) or UAMI_CLIENT_ID"
[[ "$DRY_RUN" == true || ( -n "$LOGS_INGESTION_ENDPOINT" && -n "$FINDINGS_DCR_IMMUTABLE_ID" ) ]] \
  || fail "LOGS_INGESTION_ENDPOINT and FINDINGS_DCR_IMMUTABLE_ID are required unless DRY_RUN=true (scripts/vm-package-env.sh writes them)"
[[ -z "$ANSIBLE_PLAYBOOK" || -x "$ANSIBLE_PLAYBOOK" ]] || fail "--ansible-playbook $ANSIBLE_PLAYBOOK is not executable"

[[ $EUID -eq 0 ]] || fail "run as root: sudo $0 $*"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${VERSION_ID%%.*}" == 9 ]] || fail "expected RHEL 9, got ${PRETTY_NAME:-unknown}"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PY=python3.11

# The playbook needs Ansible on this VM. python3.11 is what the role installs for the app anyway.
if [[ -z "$ANSIBLE_PLAYBOOK" ]]; then
  log "Installing $PY (dnf)"
  dnf install -y -q "$PY" "$PY-pip" tar gzip >/dev/null
  VENV=$PWD/.installer-venv
  if [[ ! -x "$VENV/bin/ansible-playbook" ]]; then
    log "Installing ansible-core into $VENV (pip${PIP_INDEX_URL:+, index $PIP_INDEX_URL})"
    "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install -q ${PIP_INDEX_URL:+--index-url "$PIP_INDEX_URL"} -r requirements-installer.txt
  fi
  ANSIBLE_PLAYBOOK=$VENV/bin/ansible-playbook
fi

# No az on the VM: IMDS issues a token for the UAMI by resource ID, and the token's appid is its client ID.
# This also proves the UAMI is attached before anything is installed.
if [[ -z "$UAMI_CLIENT_ID" ]]; then
  log "Resolving the client ID of $UAMI_RESOURCE_ID through IMDS"
  UAMI_CLIENT_ID=$(python3 - "$UAMI_RESOURCE_ID" <<'PY'
import base64, json, sys, urllib.parse, urllib.request
q = urllib.parse.urlencode({"api-version": "2018-02-01", "resource": "https://management.azure.com/",
                            "msi_res_id": sys.argv[1]})
req = urllib.request.Request("http://169.254.169.254/metadata/identity/oauth2/token?" + q,
                             headers={"Metadata": "true"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # IMDS is never behind a proxy
try:
    token = json.load(opener.open(req, timeout=10))["access_token"]
except Exception as e:  # noqa: BLE001
    sys.exit(f"IMDS token request failed: {e}. Is this UAMI attached to the VM (az vm identity assign)?")
payload = token.split(".")[1]
claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
print(claims["appid"])
PY
  )
fi

# A release id is meant to be built once. The RELEASE file inside the release directory says what it was
# built from; refuse to silently keep old code under a reused id.
REL_DIR=$INSTALL_DIR/releases/$RELEASE_ID
if [[ -f "$REL_DIR/RELEASE" ]] && ! cmp -s RELEASE "$REL_DIR/RELEASE"; then
  if [[ "$REINSTALL" == true ]]; then
    log "Replacing release $RELEASE_ID ($(grep SOURCE_COMMIT "$REL_DIR/RELEASE"))"
    rm -rf "$REL_DIR"
  else
    fail "release $RELEASE_ID is already installed from another build ($(grep SOURCE_COMMIT "$REL_DIR/RELEASE")); build a new version, or pass --reinstall"
  fi
fi

# Extra vars go through a 0600 file, not the command line: the App Insights connection string is in it.
export RELEASE_ID UAMI_CLIENT_ID
export RELEASE_BUNDLE="$PWD/release.tar.gz"
(umask 077; python3 - >"$TMP/vars.json" <<'PY'
import json, os
e = os.environ
print(json.dumps({
    "management_group_id": e["MANAGEMENT_GROUP_ID"],
    "subscription_ids": e["SUBSCRIPTION_IDS"],
    "law_resource_id": e["LAW_RESOURCE_ID"],
    "ops_schedule_cron": e["OPS_SCHEDULE_CRON"],
    "finops_schedule_cron": e["FINOPS_SCHEDULE_CRON"],
    "dry_run": e["DRY_RUN"] == "true",
    "release_ref": e["RELEASE_ID"],
    "pip_index_url": e["PIP_INDEX_URL"],
    "release_bundle": e["RELEASE_BUNDLE"],
    "uami_client_id": e["UAMI_CLIENT_ID"],
    "logs_ingestion_endpoint": e["LOGS_INGESTION_ENDPOINT"],
    "findings_dcr_immutable_id": e["FINDINGS_DCR_IMMUTABLE_ID"],
    "applicationinsights_connection_string": e["APPLICATIONINSIGHTS_CONNECTION_STRING"],
}))
PY
)
# The playbook targets the o11y_vm group; here that group is this machine. The system python has the
# dnf bindings the role's dnf task needs.
printf '[o11y_vm]\nlocalhost ansible_connection=local ansible_python_interpreter=/usr/bin/python3\n' >"$TMP/inventory.ini"

log "Running ansible/playbook.yml on this VM (release $RELEASE_ID, source $SOURCE_COMMIT)"
ANSIBLE_CONFIG=ansible/ansible.cfg ANSIBLE_ROLES_PATH=$PWD/ansible/roles \
  "$ANSIBLE_PLAYBOOK" -i "$TMP/inventory.ini" ansible/playbook.yml \
  -e "@$TMP/vars.json" ${ANSIBLE_ARGS[@]+"${ANSIBLE_ARGS[@]}"}

cat <<DONE

Installed release $RELEASE_ID (package v$PACKAGE_VERSION, source commit $SOURCE_COMMIT).
The UAMI needs Monitoring Metrics Publisher on the findings DCR (immutable id $FINDINGS_DCR_IMMUTABLE_ID), via the IAM repo.
  systemctl list-timers 'o11y-alerting-*'
  systemctl start o11y-alerting@finops.service      # run now (ops or finops)
  journalctl -u 'o11y-alerting@*' -f
DONE
