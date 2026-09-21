#!/usr/bin/env bash
# Write the package.env that install.sh (the self-contained RHEL 9 package) reads, from a vm.env, on a
# workstation with az. Resolves the UAMI client ID, creates or updates the findings tables, DCE and DCR
# (or with --no-findings-infra only looks them up), resolves the App Insights connection string and runs
# the identity check. The file goes to stdout; logs go to stderr.
#
# Usage: vm-package-env.sh --param-file vm.env [--<param> <value> ...] [--no-findings-infra]
#                          [--skip-identity-check] > package.env
#
#   --param-file           vm.env (scripts/vm.env.example); a deploy.env also works, extra keys are ignored
#   --no-findings-infra    only look up the findings DCE/DCR (use when Terraform or deploy.sh owns them)
#   --skip-identity-check  do not run scripts/check-identity.sh (it never blocks anyway)
set -euo pipefail

PARAMS=(RESOURCE_GROUP_NAME UAMI_RESOURCE_ID MANAGEMENT_GROUP_ID SUBSCRIPTION_IDS LAW_RESOURCE_ID
        OPS_SCHEDULE_CRON FINOPS_SCHEDULE_CRON DRY_RUN APP_INSIGHTS_NAME PIP_INDEX_URL)
REQUIRED=(RESOURCE_GROUP_NAME UAMI_RESOURCE_ID MANAGEMENT_GROUP_ID LAW_RESOURCE_ID)

usage() {
  sed -n '2,13p' "$0"
  echo "Parameters (flags override the file):"
  for p in "${PARAMS[@]}"; do echo "  --$(echo "$p" | tr '[:upper:]_' '[:lower:]-')"; done
}

# shellcheck source-path=SCRIPTDIR source=lib/params.sh
source "$(dirname "$0")/lib/params.sh"
# shellcheck source-path=SCRIPTDIR source=lib/findings.sh
source "$(dirname "$0")/lib/findings.sh"

PARAM_FILE=""; FINDINGS_INFRA=true; SKIP_IDENTITY_CHECK=false; OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --no-findings-infra) FINDINGS_INFRA=false; shift ;;
    --skip-identity-check) SKIP_IDENTITY_CHECK=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) OVERRIDES+=("$(flag_to_var "$1")=$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

[[ -z "$PARAM_FILE" ]] || PARAM_FILE="$(cd "$(dirname "$PARAM_FILE")" && pwd)/$(basename "$PARAM_FILE")"
cd "$(dirname "$0")/.."
if [[ -n "$PARAM_FILE" ]]; then load_params "$PARAM_FILE"; fi
# shellcheck disable=SC2163  # kv is "KEY=value"
for kv in ${OVERRIDES[@]+"${OVERRIDES[@]}"}; do export "$kv"; done

: "${OPS_SCHEDULE_CRON:=0 */15 * * * *}" "${FINOPS_SCHEDULE_CRON:=0 0 6 * * *}" "${DRY_RUN:=false}"
: "${SUBSCRIPTION_IDS:=}" "${APP_INSIGHTS_NAME:=}" "${PIP_INDEX_URL:=}"

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*" >&2; }
for r in "${REQUIRED[@]}"; do
  [[ -n "${!r:-}" ]] || fail "missing required parameter: $r"
done
[[ "$DRY_RUN" == true || "$DRY_RUN" == false ]] || fail "DRY_RUN must be true or false, got $DRY_RUN"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

log "Resolving UAMI $UAMI_RESOURCE_ID"
UAMI_CLIENT_ID=$(az identity show --ids "$UAMI_RESOURCE_ID" --query clientId -o tsv)
[[ -n "$UAMI_CLIENT_ID" ]]

if [[ "$FINDINGS_INFRA" == true ]]; then
  ensure_findings_path "$RESOURCE_GROUP_NAME" "$LAW_RESOURCE_ID" schema/findings-tables.json "$TMP"
else
  log "Looking up findings DCE $FINDINGS_DCE_NAME and DCR $FINDINGS_DCR_NAME"
  resolve_findings_path "$RESOURCE_GROUP_NAME"
fi

if [[ "$SKIP_IDENTITY_CHECK" != true ]]; then
  log "Checking UAMI role assignments"
  scripts/check-identity.sh --uami-resource-id "$UAMI_RESOURCE_ID" \
    --management-group-id "$MANAGEMENT_GROUP_ID" --law-resource-id "$LAW_RESOURCE_ID" \
    --findings-dcr-id "$DCR_ID" --skip host_storage,acr_pull >&2 || true
fi

AI_CS=""
if [[ -n "$APP_INSIGHTS_NAME" ]]; then
  AI_CS=$(az resource show -g "$RESOURCE_GROUP_NAME" -n "$APP_INSIGHTS_NAME" --resource-type microsoft.insights/components --query properties.ConnectionString -o tsv)
fi

cat <<ENV
# package.env for the self-contained RHEL 9 package, written by scripts/vm-package-env.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# Keep it 0600 when APPLICATIONINSIGHTS_CONNECTION_STRING is set.
MANAGEMENT_GROUP_ID=$MANAGEMENT_GROUP_ID
SUBSCRIPTION_IDS=$SUBSCRIPTION_IDS
LAW_RESOURCE_ID=$LAW_RESOURCE_ID
OPS_SCHEDULE_CRON=$OPS_SCHEDULE_CRON
FINOPS_SCHEDULE_CRON=$FINOPS_SCHEDULE_CRON
DRY_RUN=$DRY_RUN
PIP_INDEX_URL=$PIP_INDEX_URL
UAMI_RESOURCE_ID=$UAMI_RESOURCE_ID
UAMI_CLIENT_ID=$UAMI_CLIENT_ID
LOGS_INGESTION_ENDPOINT=$LOGS_INGESTION_ENDPOINT
FINDINGS_DCR_IMMUTABLE_ID=$FINDINGS_DCR_IMMUTABLE_ID
APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CS
ENV

log "Findings DCR (the UAMI needs Monitoring Metrics Publisher here, via the IAM repo): $DCR_ID"
