"""Azure SQL Managed Instances: Resource Graph query, parser, readiness check, recommender."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig, SqlSkuCatalog
from models import ColdFinding, Recommendation, Resource
from recommend.sqlmi import SqlMiRecommendRules, recommend_sqlmi
from resource_types.registry import ResourceTypeSpec, base_resource, require_state
from resource_types.sqldb import SQL_SKUS

KIND = "sqlmi"
ARM_TYPE = "microsoft.sql/managedinstances"
READY = "ready"

QUERY = """
resources
| where type =~ 'microsoft.sql/managedinstances'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.tier), skuName = tostring(sku.name),
          vcores = toint(properties.vCores), storageGb = toint(properties.storageSizeInGB),
          state = tostring(properties.state)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    sku_name = str(row.get("skuName") or "")
    vcores = int(row.get("vcores") or 0)
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=f"{sku_name} {vcores} vCores",
        props={
            "tier": str(row.get("tier") or ""),
            "sku_name": sku_name,
            "vcores": vcores,
            "storage_gb": int(row.get("storageGb") or 0),
            "state": str(row.get("state") or ""),
        },
    )


active = require_state("state", READY, "not_ready")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_sqlmi(
        finding,
        config.catalog_for(KIND, SqlSkuCatalog),
        config.rules_for(KIND, SqlMiRecommendRules),
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=SqlMiRecommendRules,
    catalog=SQL_SKUS,
)
