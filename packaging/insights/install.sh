#!/usr/bin/env bash
# Apply the o11y insights (Ops/FinOps workbooks, log search alerts, Teams delivery) from the package built
# by scripts/build-insights-package.sh. A thin wrapper around terraform in terraform/examples/insights that
# keeps the state and the variables OUTSIDE this versioned directory, so a newer package updates the same
# resources instead of creating them again.
#
# Usage: ./install.sh --state-dir DIR [--plan-only] [--destroy] [--yes] [-- <extra terraform plan args>]
#
#   --state-dir   holds terraform.tfvars (your settings) and terraform.tfstate. Use the same directory for
#                 every package version. The first run copies terraform.tfvars.example there and stops.
#   --plan-only   init and plan; apply nothing
#   --destroy     plan the removal of everything the module created (the findings tables are untouched)
#   --yes         apply the plan without asking
#   -- ...        passed to terraform plan, e.g. -- -target=module.insights.azurerm_application_insights_workbook.this
#
# Needs terraform >= 1.9 on PATH (or TERRAFORM=/path/to/terraform) and `az login` (or ARM_* variables) as a
# principal that may create the resources (docs/ops/insights.md, Prerequisites).
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "$*" >&2; exit 2; }
log() { echo "==> $*"; }

ROOT=terraform/examples/insights
[[ -f RELEASE && -f $ROOT/main.tf && -f terraform/module-o11y-insights/main.tf ]] \
  || fail "$PWD is not a complete package directory; extract o11y-insights-v<N>.tar.gz and run install.sh from inside it"

STATE_DIR=""; PLAN_ONLY=false; DESTROY=false; YES=false; PLAN_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --state-dir) [[ $# -ge 2 ]] || fail "--state-dir needs a value"; STATE_DIR="$2"; shift 2 ;;
    --plan-only) PLAN_ONLY=true; shift ;;
    --destroy) DESTROY=true; shift ;;
    --yes) YES=true; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    --) shift; PLAN_ARGS=("$@"); break ;;
    *) echo "unknown argument: $1" >&2; sed -n '7,7p' "$0" >&2; exit 2 ;;
  esac
done
[[ -n "$STATE_DIR" ]] || fail "--state-dir is required: a directory outside the package that keeps the state and terraform.tfvars across versions"
mkdir -p "$STATE_DIR"; STATE_DIR=$(cd "$STATE_DIR" && pwd)
case "$STATE_DIR/" in
  "$PWD"/*) fail "--state-dir must be outside the package directory, or the next version will not find the state" ;;
esac

# shellcheck disable=SC1091
. ./RELEASE
log "o11y insights package ${RELEASE_ID:-?} (source ${SOURCE_COMMIT:-?})"

TFVARS=$STATE_DIR/terraform.tfvars
if [[ ! -f "$TFVARS" ]]; then
  cp "$ROOT/terraform.tfvars.example" "$TFVARS"
  chmod 600 "$TFVARS"
  fail "Created $TFVARS from the example. Edit it (routes, IDs; see docs/ops/insights.md), then run this again."
fi

TF=${TERRAFORM:-terraform}
command -v "$TF" >/dev/null 2>&1 || fail "terraform not found; install >= 1.9 or set TERRAFORM=/path/to/terraform"
TF_VERSION=$("$TF" version | sed -n '1s/^Terraform v\([0-9][0-9.]*\).*/\1/p')
case "$TF_VERSION" in
  0.*|1.[0-8]|1.[0-8].*|"") fail "terraform >= 1.9 is required (found '${TF_VERSION:-unknown}')" ;;
esac

PLAN=$STATE_DIR/tfplan
log "terraform init (state: $STATE_DIR/terraform.tfstate)"
"$TF" -chdir="$ROOT" init -input=false -reconfigure -backend-config="path=$STATE_DIR/terraform.tfstate"

MODE=()
if [[ "$DESTROY" == true ]]; then MODE=(-destroy); fi
log "terraform plan${MODE[*]:+ ${MODE[*]}}"
"$TF" -chdir="$ROOT" plan -input=false -var-file="$TFVARS" -out="$PLAN" \
  ${MODE[@]+"${MODE[@]}"} ${PLAN_ARGS[@]+"${PLAN_ARGS[@]}"}

if [[ "$PLAN_ONLY" == true ]]; then
  log "Plan only: saved to $PLAN, nothing applied"
  exit 0
fi
if [[ "$YES" != true ]]; then
  printf 'Apply this plan? [y/N] '
  read -r answer || answer=""
  case "$answer" in y|Y|yes|YES) ;; *) rm -f "$PLAN"; log "Not applied"; exit 1 ;; esac
fi

log "terraform apply"
"$TF" -chdir="$ROOT" apply -input=false "$PLAN"
rm -f "$PLAN"
if [[ "$DESTROY" != true ]]; then
  "$TF" -chdir="$ROOT" output
  log "Every name in teams_secret_names must exist in the Key Vault before the first alert or digest."
fi
