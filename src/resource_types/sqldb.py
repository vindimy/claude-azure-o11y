"""Azure SQL databases: Resource Graph query, parser, online check, recommender binding.

`master` and data warehouses are filtered out in KQL. A database inside an elastic pool is still
evaluated on Ops runs, but FinOps skips it: its size is the pool's, not the database's.
"""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.sqldb import SqlDbRecommendRules, recommend_sqldb
from resource_types.registry import ResourceTypeSpec, parse_tags

KIND = "sqldb"
ARM_TYPE = "microsoft.sql/servers/databases"
ONLINE = "online"
# Resource Graph returns title case today, but every comparison here is case-insensitive.
DTU_TIERS = {"basic", "standard", "premium"}

QUERY = """
resources
| where type =~ 'microsoft.sql/servers/databases'
| where name !~ 'master' and tolower(kind) !contains 'datawarehouse'
| project id, name, subscriptionId, resourceGroup, location, tags, kind,
          tier = tostring(sku.tier), skuName = tostring(sku.name), capacity = toint(sku.capacity),
          status = tostring(properties.status), poolId = tostring(properties.elasticPoolId),
          maxSizeBytes = tolong(properties.maxSizeBytes)
| order by id asc
"""


def purchasing_model(tier: str, sku_name: str) -> str:
    """DTU objectives, provisioned vCores, or serverless (`_S_` in the vCore SKU name)."""
    if tier.lower() in DTU_TIERS:
        return "dtu"
    if "_S_" in sku_name.upper():
        return "serverless"
    return "vcore"


def is_hyperscale(tier: str, sku_name: str) -> bool:
    """Hyperscale storage grows on demand, so storage_percent is not reported for it."""
    return tier.lower() == "hyperscale" or sku_name.upper().startswith("HS_")


def parse(row: dict[str, Any]) -> Resource:
    tier = str(row.get("tier") or "")
    sku_name = str(row.get("skuName") or "")
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=sku_name,
        tags=parse_tags(row),
        props={
            "tier": tier,
            "sku_name": sku_name,
            "capacity": int(row.get("capacity") or 0),
            "purchasing_model": purchasing_model(tier, sku_name),
            "hyperscale": is_hyperscale(tier, sku_name),
            "pool_id": str(row.get("poolId") or ""),
            "status": str(row.get("status") or ""),
            "db_kind": str(row.get("kind") or ""),
        },
    )


def active(resource: Resource) -> Skip | None:
    status = str(resource.prop("status", ""))
    if status.lower() == ONLINE:
        return None
    return Skip(resource.id, "not_online", status or "unknown")


def finops_skip(resource: Resource) -> Skip | None:
    pool_id = str(resource.prop("pool_id", ""))
    if pool_id:
        return Skip(resource.id, "in_elastic_pool", pool_id)
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for(KIND)
    assert isinstance(rules, SqlDbRecommendRules)
    return recommend_sqldb(finding, config.sql_skus, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=SqlDbRecommendRules,
)
