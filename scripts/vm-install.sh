#!/usr/bin/env bash
# Install or update the o11y alerting runner on a provisioned RHEL 9 VM (Path C). Runs from the repo on
# your workstation: resolves the Azure-side values with az (like deploy.sh), packs the release with
# `git archive`, then runs ansible/playbook.yml against the VM. Re-run with a new --release-ref to update.
#
# Usage: vm-install.sh [--param-file vm.env] [--<param> <value> ...] [--inventory FILE]
#                      [--allow-dirty] [--no-findings-infra] [-- <extra ansible-playbook args>]
#
#   --param-file         vm.env (scripts/vm.env.example); a deploy.env also works, extra keys are ignored
#   --release-ref        commit, tag or branch to install (default HEAD). The release id on the VM is the
#                        full commit SHA, the VM's equivalent of image_tag
#   --allow-dirty        with no --release-ref and uncommitted changes, install tracked changes as
#                        dev-<short sha>-<UTC timestamp> (untracked files are not included)
#   --inventory          Ansible inventory with an `o11y_vm` group, instead of VM_HOST / VM_SSH_USER
#   --no-findings-infra  only look up the findings DCE/DCR instead of creating/updating them and the
#                        tables (use when Terraform or deploy.sh owns them)
#   -- ...               passed to ansible-playbook, e.g. -- --private-key ~/.ssh/id_vm --limit vm-01
set -euo pipefail

PARAMS=(RESOURCE_GROUP_NAME UAMI_NAME MANAGEMENT_GROUP_ID SUBSCRIPTION_IDS LAW_RESOURCE_ID
        OPS_SCHEDULE_CRON FINOPS_SCHEDULE_CRON DRY_RUN APP_INSIGHTS_NAME
        VM_HOST VM_SSH_USER RELEASE_REF PIP_INDEX_URL)
REQUIRED=(RESOURCE_GROUP_NAME UAMI_NAME MANAGEMENT_GROUP_ID LAW_RESOURCE_ID)

usage() {
  sed -n '2,18p' "$0"
  echo "Parameters (flags override the file):"
  for p in "${PARAMS[@]}"; do echo "  --$(echo "$p" | tr '[:upper:]_' '[:lower:]-')"; done
}

# shellcheck source-path=SCRIPTDIR source=lib/params.sh
source "$(dirname "$0")/lib/params.sh"
# shellcheck source-path=SCRIPTDIR source=lib/findings.sh
source "$(dirname "$0")/lib/findings.sh"

PARAM_FILE=""; INVENTORY=""; ALLOW_DIRTY=false; FINDINGS_INFRA=true
OVERRIDES=(); ANSIBLE_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --inventory) INVENTORY="$2"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=true; shift ;;
    --no-findings-infra) FINDINGS_INFRA=false; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; ANSIBLE_ARGS=("$@"); break ;;
    --*) OVERRIDES+=("$(flag_to_var "$1")=$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

# Resolve relative paths before moving to the repo root.
abspath() { echo "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"; }
[[ -z "$PARAM_FILE" ]] || PARAM_FILE=$(abspath "$PARAM_FILE")
[[ -z "$INVENTORY" ]] || INVENTORY=$(abspath "$INVENTORY")
cd "$(dirname "$0")/.."
if [[ -n "$PARAM_FILE" ]]; then load_params "$PARAM_FILE"; fi
# shellcheck disable=SC2163  # kv is "KEY=value"
for kv in ${OVERRIDES[@]+"${OVERRIDES[@]}"}; do export "$kv"; done

: "${OPS_SCHEDULE_CRON:=0 */15 * * * *}" "${FINOPS_SCHEDULE_CRON:=0 0 6 * * *}" "${DRY_RUN:=false}"
: "${SUBSCRIPTION_IDS:=}" "${APP_INSIGHTS_NAME:=}" "${VM_HOST:=}" "${VM_SSH_USER:=azureuser}"
: "${RELEASE_REF:=}" "${PIP_INDEX_URL:=}"

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*"; }
for r in "${REQUIRED[@]}"; do
  [[ -n "${!r:-}" ]] || fail "missing required parameter: $r"
done
[[ -n "$VM_HOST" || -n "$INVENTORY" ]] || fail "set VM_HOST (or --vm-host), or pass --inventory"
[[ "$DRY_RUN" == true || "$DRY_RUN" == false ]] || fail "DRY_RUN must be true or false, got $DRY_RUN"
command -v ansible-playbook >/dev/null || fail "ansible-playbook not found (pip install ansible-core)"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# Release: a commit SHA, like the image tag. A dirty tree needs --allow-dirty and gets a dev id.
if [[ -n "$RELEASE_REF" ]]; then
  COMMIT=$(git rev-parse --verify "$RELEASE_REF^{commit}") || fail "unknown --release-ref $RELEASE_REF"
  RELEASE_ID=$COMMIT
elif [[ -n "$(git status --porcelain)" ]]; then
  [[ "$ALLOW_DIRTY" == true ]] || fail "working tree has uncommitted changes; commit, or pass --allow-dirty for a dev release"
  COMMIT=$(git stash create); COMMIT=${COMMIT:-$(git rev-parse HEAD)}
  RELEASE_ID="dev-$(git rev-parse --short HEAD)-$(date -u +%Y%m%d%H%M%S)"
else
  COMMIT=$(git rev-parse HEAD)
  RELEASE_ID=$COMMIT
fi
log "Packing release $RELEASE_ID"
git archive --format=tar.gz -o "$TMP/release.tar.gz" "$COMMIT" \
  src config identity requirements.txt requirements-vm.txt

log "Resolving UAMI $UAMI_NAME"
UAMI_CLIENT_ID=$(az identity show -g "$RESOURCE_GROUP_NAME" -n "$UAMI_NAME" --query clientId -o tsv)
[[ -n "$UAMI_CLIENT_ID" ]]

if [[ "$FINDINGS_INFRA" == true ]]; then
  ensure_findings_path "$RESOURCE_GROUP_NAME" "$LAW_RESOURCE_ID" schema/findings-tables.json "$TMP"
else
  log "Looking up findings DCE $FINDINGS_DCE_NAME and DCR $FINDINGS_DCR_NAME"
  resolve_findings_path "$RESOURCE_GROUP_NAME"
fi

AI_CS=""
if [[ -n "$APP_INSIGHTS_NAME" ]]; then
  AI_CS=$(az resource show -g "$RESOURCE_GROUP_NAME" -n "$APP_INSIGHTS_NAME" --resource-type microsoft.insights/components --query properties.ConnectionString -o tsv)
fi

# Extra vars go through a 0600 file, not the command line: the App Insights connection string is in it.
export RELEASE_ID UAMI_CLIENT_ID LOGS_INGESTION_ENDPOINT FINDINGS_DCR_IMMUTABLE_ID AI_CS
export RELEASE_BUNDLE="$TMP/release.tar.gz"
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
    "applicationinsights_connection_string": e["AI_CS"],
}))
PY
)

if [[ -z "$INVENTORY" ]]; then
  INVENTORY="$TMP/inventory.ini"
  printf '[o11y_vm]\n%s ansible_user=%s\n' "$VM_HOST" "$VM_SSH_USER" >"$INVENTORY"
fi

log "Running ansible/playbook.yml"
ANSIBLE_CONFIG=ansible/ansible.cfg ansible-playbook -i "$INVENTORY" ansible/playbook.yml \
  -e "@$TMP/vars.json" ${ANSIBLE_ARGS[@]+"${ANSIBLE_ARGS[@]}"}

cat <<DONE

Installed release $RELEASE_ID.
Findings DCR (the UAMI needs Monitoring Metrics Publisher here, via the IAM repo): $DCR_ID
On the VM:
  sudo systemctl list-timers 'o11y-alerting-*'
  sudo systemctl start o11y-alerting@finops.service      # run now (ops or finops)
  sudo journalctl -u 'o11y-alerting@*' -f
DONE
