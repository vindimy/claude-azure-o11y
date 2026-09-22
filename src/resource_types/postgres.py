"""PostgreSQL Flexible Server: Resource Graph query, parser, readiness check, recommender
binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.postgres import PostgresRecommendRules, recommend_postgres
from resource_types.registry import ResourceTypeSpec, parse_tags

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
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=str(row.get("skuName") or ""),
        tags=parse_tags(row),
        props={
            "tier": str(row.get("tier") or ""),
            "state": str(row.get("state") or ""),
            "storage_gb": row.get("storageGb"),
            "version": str(row.get("version") or ""),
        },
    )


def active(resource: Resource) -> Skip | None:
    state = str(resource.prop("state", ""))
    if state.lower() == READY:
        return None
    return Skip(resource.id, "not_ready", state or "unknown")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for(KIND)
    assert isinstance(rules, PostgresRecommendRules)
    return recommend_postgres(finding, config.postgres_skus, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=PostgresRecommendRules,
)
