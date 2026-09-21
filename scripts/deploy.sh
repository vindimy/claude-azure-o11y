#!/usr/bin/env bash
# Deploy the whole o11y alerting stack with the az CLI. Idempotent. No Terraform, no docker.
set -euo pipefail

PARAMS=(RESOURCE_GROUP_NAME LOCATION UAMI_RESOURCE_ID STORAGE_ACCOUNT_NAME KEY_VAULT_NAME MANAGEMENT_GROUP_ID
        SUBSCRIPTION_IDS LAW_RESOURCE_ID ACR_NAME IMAGE_NAME IMAGE_TAG FUNCTION_APP_NAME APP_SERVICE_PLAN_NAME
        PLAN_SKU OPS_SCHEDULE_CRON FINOPS_SCHEDULE_CRON DRY_RUN APP_INSIGHTS_NAME)
REQUIRED=(RESOURCE_GROUP_NAME LOCATION UAMI_RESOURCE_ID STORAGE_ACCOUNT_NAME KEY_VAULT_NAME MANAGEMENT_GROUP_ID
          LAW_RESOURCE_ID ACR_NAME IMAGE_TAG)
SCHEMA="$(dirname "$0")/../schema/findings-tables.json"

usage() {
  cat <<USAGE
Usage: $0 [--param-file deploy.env] [--<param> <value> ...] [--skip-identity-check]
Parameters (flags override the file):
USAGE
  for p in "${PARAMS[@]}"; do echo "  --$(echo "$p" | tr '[:upper:]_' '[:lower:]-')"; done
}

PARAM_FILE=""
SKIP_IDENTITY_CHECK=false
# shellcheck source-path=SCRIPTDIR source=lib/params.sh
source "$(dirname "$0")/lib/params.sh"
# shellcheck source-path=SCRIPTDIR source=lib/findings.sh
source "$(dirname "$0")/lib/findings.sh"
OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --skip-identity-check) SKIP_IDENTITY_CHECK=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) OVERRIDES+=("$(flag_to_var "$1")=$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$PARAM_FILE" ]]; then
  load_params "$PARAM_FILE"
fi
# shellcheck disable=SC2163  # kv is "KEY=value"
for kv in ${OVERRIDES[@]+"${OVERRIDES[@]}"}; do export "$kv"; done

: "${IMAGE_NAME:=o11y-alerting}" "${FUNCTION_APP_NAME:=func-o11y-alerting}" "${APP_SERVICE_PLAN_NAME:=asp-o11y-alerting}"
: "${PLAN_SKU:=EP1}" "${OPS_SCHEDULE_CRON:=0 */15 * * * *}" "${FINOPS_SCHEDULE_CRON:=0 0 6 * * *}" "${DRY_RUN:=false}" "${SUBSCRIPTION_IDS:=}" "${APP_INSIGHTS_NAME:=}"

for r in "${REQUIRED[@]}"; do
  [[ -n "${!r:-}" ]] || { echo "missing required parameter: $r" >&2; exit 2; }
done
[[ "$PLAN_SKU" =~ ^(EP[123]|P[0-9]+v[23]|S[123]|B[123])$ ]] || { echo "PLAN_SKU must be Elastic Premium or Dedicated, got $PLAN_SKU" >&2; exit 2; }

log() { echo "==> $*"; }
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

log "Verifying image tag $IMAGE_NAME:$IMAGE_TAG exists in ACR $ACR_NAME"
az acr repository show-tags -n "$ACR_NAME" --repository "$IMAGE_NAME" -o tsv | grep -qx "$IMAGE_TAG" \
  || { echo "image tag $IMAGE_TAG not found in $ACR_NAME/$IMAGE_NAME" >&2; exit 3; }
ACR_LOGIN_SERVER=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
IMAGE_REF="$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG"

log "Resolving existing resources"
# The UAMI comes from the IAM repo and may live in another RG or subscription, so it is read by ID.
UAMI_ID=$(az identity show --ids "$UAMI_RESOURCE_ID" --query id -o tsv)
UAMI_CLIENT_ID=$(az identity show --ids "$UAMI_RESOURCE_ID" --query clientId -o tsv)
STORAGE_ID=$(az storage account show -g "$RESOURCE_GROUP_NAME" -n "$STORAGE_ACCOUNT_NAME" --query id -o tsv)
KV_ID=$(az keyvault show -n "$KEY_VAULT_NAME" --query id -o tsv)
[[ -n "$UAMI_ID" && -n "$STORAGE_ID" && -n "$KV_ID" ]]

ensure_findings_path "$RESOURCE_GROUP_NAME" "$LAW_RESOURCE_ID" "$SCHEMA" "$TMP"

log "Ensuring plan $APP_SERVICE_PLAN_NAME ($PLAN_SKU)"
if ! az functionapp plan show -g "$RESOURCE_GROUP_NAME" -n "$APP_SERVICE_PLAN_NAME" >/dev/null 2>&1; then
  az functionapp plan create -g "$RESOURCE_GROUP_NAME" -n "$APP_SERVICE_PLAN_NAME" -l "$LOCATION" --sku "$PLAN_SKU" --is-linux -o none
fi

log "Ensuring function app $FUNCTION_APP_NAME"
if ! az functionapp show -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" >/dev/null 2>&1; then
  az functionapp create -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" -p "$APP_SERVICE_PLAN_NAME" \
    --storage-account "$STORAGE_ACCOUNT_NAME" --functions-version 4 --os-type Linux \
    --image "$IMAGE_REF" --assign-identity "$UAMI_ID" -o none
fi

log "Attaching UAMI and configuring ACR pull + Key Vault reference identity"
az functionapp identity assign -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" --identities "$UAMI_ID" -o none
SITE_ID=$(az functionapp show -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" --query id -o tsv)
az resource update --ids "$SITE_ID" --set "properties.keyVaultReferenceIdentity=$UAMI_ID" -o none
az resource update --ids "$SITE_ID/config/web" \
  --set properties.acrUseManagedIdentityCreds=true "properties.acrUserManagedIdentityID=$UAMI_ID" \
        "properties.linuxFxVersion=DOCKER|$IMAGE_REF" -o none

log "Setting app settings (identity-based host storage, findings ingestion)"
SETTINGS=(
  "FUNCTIONS_WORKER_RUNTIME=python"
  "FUNCTIONS_EXTENSION_VERSION=~4"
  "WEBSITES_ENABLE_APP_SERVICE_STORAGE=false"
  "AzureWebJobsStorage__accountName=$STORAGE_ACCOUNT_NAME"
  "AzureWebJobsStorage__credential=managedidentity"
  "AzureWebJobsStorage__clientId=$UAMI_CLIENT_ID"
  "AZURE_CLIENT_ID=$UAMI_CLIENT_ID"
  "MG_ID=$MANAGEMENT_GROUP_ID"
  "SUBSCRIPTION_IDS=$SUBSCRIPTION_IDS"
  "LAW_RESOURCE_ID=$LAW_RESOURCE_ID"
  "OPS_SCHEDULE_CRON=$OPS_SCHEDULE_CRON"
  "FINOPS_SCHEDULE_CRON=$FINOPS_SCHEDULE_CRON"
  "DRY_RUN=$DRY_RUN"
  "CONFIG_DIR=config"
  "IDENTITY_FILE=identity/role-requirements.yaml"
  "LOGS_INGESTION_ENDPOINT=$LOGS_INGESTION_ENDPOINT"
  "FINDINGS_DCR_IMMUTABLE_ID=$FINDINGS_DCR_IMMUTABLE_ID"
)
if [[ -n "$APP_INSIGHTS_NAME" ]]; then
  AI_CS=$(az resource show -g "$RESOURCE_GROUP_NAME" -n "$APP_INSIGHTS_NAME" --resource-type microsoft.insights/components --query properties.ConnectionString -o tsv)
  SETTINGS+=("APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CS")
fi
az functionapp config appsettings set -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" --settings "${SETTINGS[@]}" -o none
# Remove key-based storage settings that `functionapp create` may have added.
az functionapp config appsettings delete -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" \
  --setting-names AzureWebJobsStorage WEBSITE_CONTENTAZUREFILECONNECTIONSTRING WEBSITE_CONTENTSHARE -o none 2>/dev/null || true


if [[ "$SKIP_IDENTITY_CHECK" != true ]]; then
  log "Checking UAMI role assignments"
  "$(dirname "$0")/check-identity.sh" --uami-resource-id "$UAMI_ID" \
    --management-group-id "$MANAGEMENT_GROUP_ID" --storage-account-id "$STORAGE_ID" --key-vault-id "$KV_ID" \
    --acr-name "$ACR_NAME" --law-resource-id "$LAW_RESOURCE_ID" --findings-dcr-id "$DCR_ID" || true
fi

log "Restarting app so the new image is pulled"
az functionapp restart -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" -o none

HOST=$(az functionapp show -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" --query defaultHostName -o tsv)
cat <<DONE

Deployed $IMAGE_REF to https://$HOST
Findings DCR (grant the UAMI Monitoring Metrics Publisher here, via the IAM repo): $DCR_ID
Manual trigger (needs the master key; functions o11y_ops and o11y_finops):
  KEY=\$(az functionapp keys list -g $RESOURCE_GROUP_NAME -n $FUNCTION_APP_NAME --query masterKey -o tsv)
  curl -X POST "https://$HOST/admin/functions/o11y_finops" -H "x-functions-key: \$KEY" -H "Content-Type: application/json" -d '{}'
DONE
