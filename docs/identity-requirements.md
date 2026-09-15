# UAMI role requirements

Generated from `identity/role-requirements.yaml` by `make identity-doc`. Do not edit by hand.

| Need | Role | Scope type | Scope | Purpose |
|---|---|---|---|---|
| `inventory` | Reader | management_group | `{management_group_id}` | Resource Graph inventory (types, regions, SKUs, tags, power state) |
| `metrics` | Monitoring Reader | management_group | `{management_group_id}` | Platform metrics via the Metrics Batch API (metrics:getBatch) |
| `law` | Log Analytics Reader | log_analytics_workspace | `{law_resource_id}` | VM guest metrics (memory, disk) via KQL. Not used by the MVP; requested now to avoid a second IAM round-trip |
| `reports` | Storage Blob Data Contributor | storage_container | `{storage_account_id}/blobServices/default/containers/reports` | Upload FinOps Markdown reports |
| `suppression` | Storage Blob Data Contributor | storage_container | `{storage_account_id}/blobServices/default/containers/suppression` | Read/write the Ops alert suppression cache |
| `host_storage` | Storage Blob Data Owner | storage_account | `{storage_account_id}` | Azure Functions host storage (AzureWebJobsStorage) over identity; timer-trigger leases and host state. Required because keys and connection strings are prohibited |
| `secrets` | Key Vault Secrets User | key_vault | `{key_vault_id}` | Resolve Key Vault references for Teams webhook URLs (and SMTP later) |
| `acr_pull` | AcrPull | container_registry | `{acr_id}` | Pull the o11y-alerting function image |
