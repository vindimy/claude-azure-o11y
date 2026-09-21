# shellcheck shell=bash
# Findings path, shared by deploy.sh and vm-install.sh: custom tables in the LAW, plus a DCE and DCR in the
# LAW's region. Bodies are rendered from schema/findings-tables.json, the same file the Terraform module
# reads. API versions are pinned (docs/gotchas.md). Callers define log().

# Same names as terraform/module-azure-o11y/locals.tf.
FINDINGS_DCE_NAME=dce-o11y-findings
FINDINGS_DCR_NAME=dcr-o11y-findings
ARM=https://management.azure.com

# findings_ids RG: sets DCE_ID and DCR_ID.
findings_ids() {
  local rg_id
  rg_id=$(az group show -n "$1" --query id -o tsv)
  DCE_ID="$rg_id/providers/Microsoft.Insights/dataCollectionEndpoints/$FINDINGS_DCE_NAME"
  DCR_ID="$rg_id/providers/Microsoft.Insights/dataCollectionRules/$FINDINGS_DCR_NAME"
}

# ensure_findings_path RG LAW_RESOURCE_ID SCHEMA TMPDIR: create or update tables, DCE and DCR; then
# resolve_findings_path.
ensure_findings_path() {
  local rg=$1 law_id=$2 schema=$3 tmp=$4 law_location tables t table_url state
  law_location=$(az resource show --ids "$law_id" --query location -o tsv)
  [[ -n "$law_location" ]]

  log "Ensuring findings tables in $law_id"
  tables=$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["tables"]))' "$schema")
  for t in $tables; do
    python3 - "$schema" "$t" >"$tmp/$t.json" <<'PY'
import json, sys
spec = json.load(open(sys.argv[1]))["tables"][sys.argv[2]]
cols = [{"name": c["name"], "type": "dateTime" if c["type"] == "datetime" else c["type"],
         "description": c["description"]} for c in spec["columns"]]
print(json.dumps({"properties": {"plan": "Analytics", "schema": {
    "name": sys.argv[2], "description": spec["description"], "columns": cols}}}))
PY
    table_url="$ARM$law_id/tables/$t?api-version=2022-10-01"
    az rest --method put --url "$table_url" --body "@$tmp/$t.json" -o none
    # Table PUT is asynchronous; the DCR rejects output streams whose table is not provisioned yet.
    for _ in $(seq 60); do
      state=$(az rest --method get --url "$table_url" --query properties.provisioningState -o tsv)
      [[ "$state" == Succeeded ]] && break
      sleep 5
    done
    [[ "$state" == Succeeded ]] || { echo "table $t not provisioned (state: $state)" >&2; exit 4; }
  done

  log "Ensuring DCE $FINDINGS_DCE_NAME and DCR $FINDINGS_DCR_NAME ($law_location)"
  findings_ids "$rg"
  az rest --method put --url "$ARM$DCE_ID?api-version=2023-03-11" -o none \
    --body "{\"location\": \"$law_location\", \"tags\": {\"workload\": \"o11y-alerting\"}, \"properties\": {\"networkAcls\": {\"publicNetworkAccess\": \"Enabled\"}}}"
  python3 - "$schema" "$law_location" "$DCE_ID" "$law_id" >"$tmp/dcr.json" <<'PY'
import json, sys
schema, location, dce_id, law_id = sys.argv[1:5]
tables = json.load(open(schema))["tables"]
print(json.dumps({"location": location, "tags": {"workload": "o11y-alerting"}, "properties": {
    "dataCollectionEndpointId": dce_id,
    "streamDeclarations": {f"Custom-{t}": {"columns": [{"name": c["name"], "type": c["type"]}
                                                       for c in spec["columns"]]}
                           for t, spec in tables.items()},
    "destinations": {"logAnalytics": [{"name": "law", "workspaceResourceId": law_id}]},
    "dataFlows": [{"streams": [f"Custom-{t}"], "destinations": ["law"], "transformKql": "source",
                   "outputStream": f"Custom-{t}"} for t in tables],
}}))
PY
  az rest --method put --url "$ARM$DCR_ID?api-version=2023-03-11" --body "@$tmp/dcr.json" -o none
  resolve_findings_path "$rg"
}

# resolve_findings_path RG: read existing DCE/DCR; sets DCE_ID, DCR_ID, LOGS_INGESTION_ENDPOINT and
# FINDINGS_DCR_IMMUTABLE_ID.
resolve_findings_path() {
  findings_ids "$1"
  LOGS_INGESTION_ENDPOINT=$(az rest --method get --url "$ARM$DCE_ID?api-version=2023-03-11" --query properties.logsIngestion.endpoint -o tsv)
  FINDINGS_DCR_IMMUTABLE_ID=$(az rest --method get --url "$ARM$DCR_ID?api-version=2023-03-11" --query properties.immutableId -o tsv)
  [[ -n "$LOGS_INGESTION_ENDPOINT" && -n "$FINDINGS_DCR_IMMUTABLE_ID" ]] \
    || { echo "findings DCE/DCR not found in RG $1" >&2; exit 4; }
}
