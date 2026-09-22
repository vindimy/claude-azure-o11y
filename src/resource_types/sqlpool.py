"""Azure SQL elastic pools: Resource Graph query, parser, readiness check, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.sqlpool import SqlPoolRecommendRules, recommend_sqlpool
from resource_types.registry import ResourceTypeSpec, parse_tags

KIND = "sqlpool"
ARM_TYPE = "microsoft.sql/servers/elasticpools"
READY = "ready"
DTU_TIERS = {"basic", "standard", "premium"}

QUERY = """
resources
| where type =~ 'microsoft.sql/servers/elasticpools'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.tier), skuName = tostring(sku.name), capacity = toint(sku.capacity),
          state = tostring(properties.state)
| order by id asc
"""


def _purchasing_model(tier: str) -> str:
    return "dtu" if tier.lower() in DTU_TIERS else "vcore"


def parse(row: dict[str, Any]) -> Resource:
    tier = str(row.get("tier") or "")
    sku_name = str(row.get("skuName") or "")
    capacity = int(row.get("capacity") or 0)
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=f"{sku_name} {capacity}",
        tags=parse_tags(row),
        props={
            "tier": tier,
            "sku_name": sku_name,
            "capacity": capacity,
            "purchasing_model": _purchasing_model(tier),
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
    assert isinstance(rules, SqlPoolRecommendRules)
    return recommend_sqlpool(finding, config.sql_skus, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=SqlPoolRecommendRules,
    priced=False,
)
