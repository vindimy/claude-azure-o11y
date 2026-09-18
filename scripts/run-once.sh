#!/usr/bin/env bash
# Run one evaluation locally in DRY_RUN mode against one subscription (or the MG). Uses `az login` credentials.
# Usage: run-once.sh --mg-id mg-x [--subscription-ids "s1,s2"] [--mode ops|finops|all] [--output-dir ./out]
set -euo pipefail
cd "$(dirname "$0")/.."
export DRY_RUN=true OUTPUT_DIR=./out CONFIG_DIR=config IDENTITY_FILE=identity/role-requirements.yaml RUN_MODES=ops,finops
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mg-id) export MG_ID="$2"; shift 2 ;;
    --subscription-ids) export SUBSCRIPTION_IDS="$2"; shift 2 ;;
    --output-dir) export OUTPUT_DIR="$2"; shift 2 ;;
    --mode) case "$2" in all) RUN_MODES=ops,finops ;; ops|finops) RUN_MODES="$2" ;; *) echo "--mode must be ops, finops or all" >&2; exit 2 ;; esac; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
: "${MG_ID:?--mg-id is required}"
PY=${PY:-.venv/bin/python}
exec "$PY" src/run_local.py
