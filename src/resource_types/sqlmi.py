"""Azure SQL Managed Instances: Resource Graph query, parser, readiness check, recommender."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.sqlmi import SqlMiRecommendRules, recommend_sqlmi
from resource_types.registry import ResourceTypeSpec, parse_tags

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
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=f"{sku_name} {vcores} vCores",
        tags=parse_tags(row),
        props={
            "tier": str(row.get("tier") or ""),
            "sku_name": sku_name,
            "vcores": vcores,
            "storage_gb": int(row.get("storageGb") or 0),
            "state": str(row.get("state") or ""),
        },
    )


def active(resource: Resource) -> Skip | None:
    state = str(resource.prop("state", ""))
    if state.lower() == READY:
        return None
    return Skip(resource.id, "not_ready", state or "unknown")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for(KIND)
    assert isinstance(rules, SqlMiRecommendRules)
    return recommend_sqlmi(finding, config.sql_skus, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=SqlMiRecommendRules,
    priced=False,
)
