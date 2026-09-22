"""PostgreSQL Flexible Server: Resource Graph query, parser, readiness check, recommender
binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig, FamilySkuCatalog
from models import ColdFinding, Recommendation, Resource
from recommend.postgres import PostgresRecommendRules, recommend_postgres
from resource_types.registry import CatalogSource, ResourceTypeSpec, base_resource, require_state

KIND = "postgres"
ARM_TYPE = "microsoft.dbforpostgresql/flexibleservers"
READY = "ready"

QUERY = """
resources
| where type =~ 'microsoft.dbforpostgresql/flexibleservers'
| project id, name, subscriptionId, resourceGroup, location, tags,
          skuName = tostring(sku.name), tier = tostring(sku.tier),
          state = tostring(properties.state), version = tostring(properties.version),
          storageGb = toint(properties.storage.storageSizeGB)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=str(row.get("skuName") or ""),
        props={
            "tier": str(row.get("tier") or ""),
            "state": str(row.get("state") or ""),
            "storage_gb": row.get("storageGb"),
            "version": str(row.get("version") or ""),
        },
    )


active = require_state("state", READY, "not_ready")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_postgres(
        finding,
        config.catalog_for(KIND, FamilySkuCatalog),
        config.rules_for(KIND, PostgresRecommendRules),
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=PostgresRecommendRules,
    catalog=CatalogSource("postgres-skus.yaml", FamilySkuCatalog),
)
