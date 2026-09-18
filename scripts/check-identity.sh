#!/usr/bin/env bash
# Verify the UAMI holds every row in identity/role-requirements.yaml. Prints missing rows; exit 1 if any.
# Usage: check-identity.sh --uami-name X --resource-group RG --management-group-id MG --storage-account-id ID \
#        --key-vault-id ID --acr-name NAME [--law-resource-id ID] [--findings-dcr-id ID]
set -euo pipefail
LAW_RESOURCE_ID=""
FINDINGS_DCR_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --uami-name) UAMI_NAME="$2"; shift 2 ;;
    --resource-group) RESOURCE_GROUP_NAME="$2"; shift 2 ;;
    --management-group-id) MANAGEMENT_GROUP_ID="$2"; shift 2 ;;
    --storage-account-id) STORAGE_ACCOUNT_ID="$2"; shift 2 ;;
    --key-vault-id) KEY_VAULT_ID="$2"; shift 2 ;;
    --acr-name) ACR_NAME="$2"; shift 2 ;;
    --law-resource-id) LAW_RESOURCE_ID="$2"; shift 2 ;;
    --findings-dcr-id) FINDINGS_DCR_ID="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
: "${UAMI_NAME:?}" "${RESOURCE_GROUP_NAME:?}" "${MANAGEMENT_GROUP_ID:?}" "${STORAGE_ACCOUNT_ID:?}" "${KEY_VAULT_ID:?}" "${ACR_NAME:?}"
PRINCIPAL_ID=$(az identity show -g "$RESOURCE_GROUP_NAME" -n "$UAMI_NAME" --query principalId -o tsv)
ACR_ID=$(az acr show -n "$ACR_NAME" --query id -o tsv)
MG_SCOPE="/providers/Microsoft.Management/managementGroups/$MANAGEMENT_GROUP_ID"
YAML="$(dirname "$0")/../identity/role-requirements.yaml"

missing=0
while IFS=$'\t' read -r id role scope; do
  scope=${scope//\{management_group_id\}/$MG_SCOPE}
  scope=${scope//\{storage_account_id\}/$STORAGE_ACCOUNT_ID}
  scope=${scope//\{key_vault_id\}/$KEY_VAULT_ID}
  scope=${scope//\{acr_id\}/$ACR_ID}
  scope=${scope//\{law_resource_id\}/$LAW_RESOURCE_ID}
  scope=${scope//\{findings_dcr_id\}/$FINDINGS_DCR_ID}
  if [[ "$id" == "law" && -z "$LAW_RESOURCE_ID" ]]; then echo "skip  $id (no LAW configured)"; continue; fi
  if [[ "$id" == "findings_ingest" && -z "$FINDINGS_DCR_ID" ]]; then echo "skip  $id (no findings DCR given)"; continue; fi
  # --scope matches assignments at that scope or above (inherited), which is what we want.
  if az role assignment list --assignee "$PRINCIPAL_ID" --scope "$scope" --query "[?roleDefinitionName=='$role'] | length(@)" -o tsv | grep -qv '^0$'; then
    echo "ok    $id: $role on $scope"
  else
    echo "MISSING $id: $role on $scope"; missing=$((missing+1))
  fi
done < <(python3 - "$YAML" <<'PY'
import sys, yaml
for r in yaml.safe_load(open(sys.argv[1]))["requirements"]:
    print(f"{r['id']}\t{r['role']}\t{r['scope']}")
PY
)
[[ $missing -eq 0 ]] || { echo "$missing role(s) missing - request them via the IAM repo using identity/role-requirements.yaml"; exit 1; }
