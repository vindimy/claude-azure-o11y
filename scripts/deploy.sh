#!/usr/bin/env bash
# Deploy the whole o11y alerting stack with the az CLI. Idempotent. No Terraform, no docker.
set -euo pipefail

PARAMS=(RESOURCE_GROUP_NAME LOCATION UAMI_NAME STORAGE_ACCOUNT_NAME KEY_VAULT_NAME MANAGEMENT_GROUP_ID
        SUBSCRIPTION_IDS LAW_RESOURCE_ID ACR_NAME IMAGE_NAME IMAGE_TAG FUNCTION_APP_NAME APP_SERVICE_PLAN_NAME
        PLAN_SKU SCHEDULE_CRON DRY_RUN OPS_WEBHOOK_SECRET_NAME APP_INSIGHTS_NAME)
REQUIRED=(RESOURCE_GROUP_NAME LOCATION UAMI_NAME STORAGE_ACCOUNT_NAME KEY_VAULT_NAME MANAGEMENT_GROUP_ID
          ACR_NAME IMAGE_TAG OPS_WEBHOOK_SECRET_NAME)

usage() {
  cat <<USAGE
Usage: $0 [--param-file deploy.env] [--<param> <value> ...] [--skip-identity-check]
Parameters (flags override the file):
USAGE
  for p in "${PARAMS[@]}"; do echo "  --$(echo "$p" | tr '[:upper:]_' '[:lower:]-')"; done
}

PARAM_FILE=""
SKIP_IDENTITY_CHECK=false
declare -A OVERRIDES=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --param-file) PARAM_FILE="$2"; shift 2 ;;
    --skip-identity-check) SKIP_IDENTITY_CHECK=true; shift ;;
    -h|--help) usage; exit 0 ;;
    --*) key=$(echo "${1#--}" | tr '[:lower:]-' '[:upper:]_'); OVERRIDES["$key"]="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ -n "$PARAM_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source <(grep -v '^\s*#' "$PARAM_FILE" | sed 's/\s*#.*$//'); set +a
fi
for k in "${!OVERRIDES[@]}"; do export "$k=${OVERRIDES[$k]}"; done

: "${IMAGE_NAME:=o11y-alerting}" "${FUNCTION_APP_NAME:=func-o11y-alerting}" "${APP_SERVICE_PLAN_NAME:=asp-o11y-alerting}"
: "${PLAN_SKU:=EP1}" "${SCHEDULE_CRON:=0 */15 * * * *}" "${DRY_RUN:=false}" "${SUBSCRIPTION_IDS:=}" "${LAW_RESOURCE_ID:=}" "${APP_INSIGHTS_NAME:=}"

for r in "${REQUIRED[@]}"; do
  [[ -n "${!r:-}" ]] || { echo "missing required parameter: $r" >&2; exit 2; }
done
[[ "$PLAN_SKU" =~ ^(EP[123]|P[0-9]+v[23]|S[123]|B[123])$ ]] || { echo "PLAN_SKU must be Elastic Premium or Dedicated, got $PLAN_SKU" >&2; exit 2; }

log() { echo "==> $*"; }

log "Verifying image tag $IMAGE_NAME:$IMAGE_TAG exists in ACR $ACR_NAME"
az acr repository show-tags -n "$ACR_NAME" --repository "$IMAGE_NAME" -o tsv | grep -qx "$IMAGE_TAG" \
  || { echo "image tag $IMAGE_TAG not found in $ACR_NAME/$IMAGE_NAME" >&2; exit 3; }
ACR_LOGIN_SERVER=$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)
IMAGE_REF="$ACR_LOGIN_SERVER/$IMAGE_NAME:$IMAGE_TAG"

log "Resolving existing resources"
UAMI_ID=$(az identity show -g "$RESOURCE_GROUP_NAME" -n "$UAMI_NAME" --query id -o tsv)
UAMI_CLIENT_ID=$(az identity show -g "$RESOURCE_GROUP_NAME" -n "$UAMI_NAME" --query clientId -o tsv)
STORAGE_ID=$(az storage account show -g "$RESOURCE_GROUP_NAME" -n "$STORAGE_ACCOUNT_NAME" --query id -o tsv)
KV_ID=$(az keyvault show -n "$KEY_VAULT_NAME" --query id -o tsv)
[[ -n "$UAMI_ID" && -n "$STORAGE_ID" && -n "$KV_ID" ]]

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

log "Setting app settings (Key Vault references, identity-based host storage)"
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
  "STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT_NAME"
  "SCHEDULE_CRON=$SCHEDULE_CRON"
  "DRY_RUN=$DRY_RUN"
  "CONFIG_DIR=config"
  "IDENTITY_FILE=identity/role-requirements.yaml"
  "OPS_TEAMS_WEBHOOK_URL=@Microsoft.KeyVault(VaultName=$KEY_VAULT_NAME;SecretName=$OPS_WEBHOOK_SECRET_NAME)"
)
if [[ -n "$APP_INSIGHTS_NAME" ]]; then
  AI_CS=$(az resource show -g "$RESOURCE_GROUP_NAME" -n "$APP_INSIGHTS_NAME" --resource-type microsoft.insights/components --query properties.ConnectionString -o tsv)
  SETTINGS+=("APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CS")
fi
az functionapp config appsettings set -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" --settings "${SETTINGS[@]}" -o none
# Remove key-based storage settings that `functionapp create` may have added.
az functionapp config appsettings delete -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" \
  --setting-names AzureWebJobsStorage WEBSITE_CONTENTAZUREFILECONNECTIONSTRING WEBSITE_CONTENTSHARE -o none 2>/dev/null || true

log "Ensuring blob containers reports/ and suppression/"
for c in reports suppression; do
  az storage container create --account-name "$STORAGE_ACCOUNT_NAME" -n "$c" --auth-mode login -o none
done

if [[ "$SKIP_IDENTITY_CHECK" != true ]]; then
  log "Checking UAMI role assignments"
  "$(dirname "$0")/check-identity.sh" --uami-name "$UAMI_NAME" --resource-group "$RESOURCE_GROUP_NAME" \
    --management-group-id "$MANAGEMENT_GROUP_ID" --storage-account-id "$STORAGE_ID" --key-vault-id "$KV_ID" \
    --acr-name "$ACR_NAME" ${LAW_RESOURCE_ID:+--law-resource-id "$LAW_RESOURCE_ID"} || true
fi

log "Restarting app so the new image is pulled"
az functionapp restart -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" -o none

HOST=$(az functionapp show -g "$RESOURCE_GROUP_NAME" -n "$FUNCTION_APP_NAME" --query defaultHostName -o tsv)
cat <<DONE

Deployed $IMAGE_REF to https://$HOST
Manual trigger (needs the master key):
  KEY=\$(az functionapp keys list -g $RESOURCE_GROUP_NAME -n $FUNCTION_APP_NAME --query masterKey -o tsv)
  curl -X POST "https://$HOST/admin/functions/o11y_evaluate" -H "x-functions-key: \$KEY" -H "Content-Type: application/json" -d '{}'
DONE
