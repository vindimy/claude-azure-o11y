#!/usr/bin/env bash
# Remove only what deploy.sh created: function app, plan, findings DCR/DCE, and on request the
# findings tables with their data (--with-tables).
set -euo pipefail
PARAM_FILE=""; WITH_TABLES=false
# shellcheck source-path=SCRIPTDIR source=lib/params.sh
source "$(dirname "$0")/lib/params.sh"
OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --with-tables) WITH_TABLES=true; shift ;;
    --*) OVERRIDES+=("$(flag_to_var "$1")=$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$PARAM_FILE" ]]; then load_params "$PARAM_FILE"; fi
# shellcheck disable=SC2163  # kv is "KEY=value"
for kv in ${OVERRIDES[@]+"${OVERRIDES[@]}"}; do export "$kv"; done
: "${FUNCTION_APP_NAME:=func-o11y-alerting}" "${APP_SERVICE_PLAN_NAME:=asp-o11y-alerting}"
: "${RESOURCE_GROUP_NAME:?}"

az functionapp delete -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" 2>/dev/null || echo "function app already absent"
az functionapp plan delete -g "$RESOURCE_GROUP_NAME" -n "$APP_SERVICE_PLAN_NAME" --yes 2>/dev/null || echo "plan already absent"
RG_ID=$(az group show -n "$RESOURCE_GROUP_NAME" --query id -o tsv)
for r in dataCollectionRules/dcr-o11y-findings dataCollectionEndpoints/dce-o11y-findings; do
  az rest --method delete --url "https://management.azure.com$RG_ID/providers/Microsoft.Insights/$r?api-version=2023-03-11" -o none \
    || echo "$r already absent"
done
if [[ "$WITH_TABLES" == true ]]; then
  : "${LAW_RESOURCE_ID:?--with-tables needs LAW_RESOURCE_ID}"
  for t in O11yOpsFindings_CL O11yFinOpsFindings_CL; do
    az rest --method delete --url "https://management.azure.com$LAW_RESOURCE_ID/tables/$t?api-version=2022-10-01" -o none || true
  done
fi
echo "done"
