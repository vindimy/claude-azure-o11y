#!/usr/bin/env bash
# Run one evaluation locally with `az login` credentials. Dry run by default: nothing is written to Log
# Analytics and rows land in OUTPUT_DIR/findings/<table>.jsonl. --live resolves the findings DCE/DCR in
# RESOURCE_GROUP_NAME (same lookup as deploy.sh / vm-install.sh) and writes to the workspace; your user
# then needs Monitoring Metrics Publisher on that DCR.
#
# Usage: run-once.sh [--param-file deploy.env|vm.env] [--mg-id mg-x] [--subscription-ids "s1,s2"]
#                    [--resource-group rg] [--mode ops|finops|all] [--output-dir ./out] [--live]
#
#   --param-file   reads MANAGEMENT_GROUP_ID, SUBSCRIPTION_IDS and RESOURCE_GROUP_NAME; flags win
#   --mg-id        required unless the param file (or MG_ID in the environment) provides it
#   --live         DRY_RUN=false; needs --resource-group (or RESOURCE_GROUP_NAME in the param file)
set -euo pipefail

PARAM_FILE=""; LIVE=false; ARG_MG_ID=""; ARG_SUBS=""; ARG_RG=""
OUTPUT_DIR=./out; RUN_MODES=ops,finops
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --mg-id) ARG_MG_ID="$2"; shift 2 ;;
    --subscription-ids) ARG_SUBS="$2"; shift 2 ;;
    --resource-group) ARG_RG="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --mode) case "$2" in all) RUN_MODES=ops,finops ;; ops|finops) RUN_MODES="$2" ;; *) echo "--mode must be ops, finops or all" >&2; exit 2 ;; esac; shift 2 ;;
    --live) LIVE=true; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# Resolve the param file before moving to the repo root, so relative paths keep working.
[[ -z "$PARAM_FILE" ]] || PARAM_FILE="$(cd "$(dirname "$PARAM_FILE")" && pwd)/$(basename "$PARAM_FILE")"
cd "$(dirname "$0")/.."
# shellcheck source-path=SCRIPTDIR source=lib/params.sh
source scripts/lib/params.sh
# shellcheck source-path=SCRIPTDIR source=lib/findings.sh
source scripts/lib/findings.sh
log() { echo "==> $*" >&2; }

if [[ -n "$PARAM_FILE" ]]; then load_params "$PARAM_FILE"; fi
export MG_ID="${ARG_MG_ID:-${MANAGEMENT_GROUP_ID:-${MG_ID:-}}}"
export SUBSCRIPTION_IDS="${ARG_SUBS:-${SUBSCRIPTION_IDS:-}}"
RESOURCE_GROUP_NAME="${ARG_RG:-${RESOURCE_GROUP_NAME:-}}"
[[ -n "$MG_ID" ]] || { echo "--mg-id is required (or MANAGEMENT_GROUP_ID in --param-file)" >&2; exit 2; }
export OUTPUT_DIR RUN_MODES CONFIG_DIR=config IDENTITY_FILE=identity/role-requirements.yaml

if [[ "$LIVE" == true ]]; then
  [[ -n "$RESOURCE_GROUP_NAME" ]] || { echo "--live needs --resource-group (or RESOURCE_GROUP_NAME in --param-file)" >&2; exit 2; }
  log "Resolving findings DCE/DCR in $RESOURCE_GROUP_NAME"
  resolve_findings_path "$RESOURCE_GROUP_NAME"
  export DRY_RUN=false LOGS_INGESTION_ENDPOINT FINDINGS_DCR_IMMUTABLE_ID
  log "LIVE: writing findings for $MG_ID to the workspace through $DCR_ID"
else
  export DRY_RUN=true
fi

PY=${PY:-.venv/bin/python}
exec "$PY" src/run_local.py
