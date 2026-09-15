#!/usr/bin/env bash
# Remove only what deploy.sh created: function app, plan, and (on request) the two blob containers.
set -euo pipefail
PARAM_FILE=""; WITH_CONTAINERS=false
declare -A OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --with-containers) WITH_CONTAINERS=true; shift ;;
    --*) key=$(echo "${1#--}" | tr '[:lower:]-' '[:upper:]_'); OVERRIDES["$key"]="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$PARAM_FILE" ]]; then set -a; source <(grep -v '^\s*#' "$PARAM_FILE" | sed 's/\s*#.*$//'); set +a; fi
for k in "${!OVERRIDES[@]}"; do export "$k=${OVERRIDES[$k]}"; done
: "${FUNCTION_APP_NAME:=func-o11y-alerting}" "${APP_SERVICE_PLAN_NAME:=asp-o11y-alerting}"
: "${RESOURCE_GROUP_NAME:?}" "${STORAGE_ACCOUNT_NAME:?}"

az functionapp delete -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" 2>/dev/null || echo "function app already absent"
az functionapp plan delete -g "$RESOURCE_GROUP_NAME" -n "$APP_SERVICE_PLAN_NAME" --yes 2>/dev/null || echo "plan already absent"
if [[ "$WITH_CONTAINERS" == true ]]; then
  for c in reports suppression; do
    az storage container delete --account-name "$STORAGE_ACCOUNT_NAME" -n "$c" --auth-mode login -o none || true
  done
fi
echo "done"
