"""Azure Cache for Redis: Resource Graph query, parser, readiness check, recommender binding.

Covers `microsoft.cache/redis` (Basic, Standard, Premium). Enterprise tiers are a different ARM
type (`microsoft.cache/redisenterprise`) with their own SKUs and metrics; they are out of scope.
"""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource
from recommend.redis import RedisRecommendRules, RedisSkuCatalog, recommend_redis
from resource_types.registry import CatalogSource, ResourceTypeSpec, base_resource, require_state

KIND = "redis"
ARM_TYPE = "microsoft.cache/redis"
READY = "Succeeded"

QUERY = """
resources
| where type =~ 'microsoft.cache/redis'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(properties.sku.name), family = tostring(properties.sku.family),
          capacity = toint(properties.sku.capacity),
          state = tostring(properties.provisioningState),
          shards = toint(properties.shardCount),
          redisVersion = tostring(properties.redisVersion)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    tier = str(row.get("tier") or "")
    family = str(row.get("family") or "")
    capacity = int(row.get("capacity") or 0)
    size = f"{family}{capacity}" if family else ""
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=f"{tier} {size}".strip(),
        props={
            "tier": tier,
            "family": family,
            "capacity": capacity,
            "size": size,
            "state": str(row.get("state") or ""),
            "shards": int(row.get("shards") or 0),
            "redis_version": str(row.get("redisVersion") or ""),
        },
    )


active = require_state("state", READY, "not_ready")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_redis(
        finding,
        config.catalog_for(KIND, RedisSkuCatalog),
        config.rules_for(KIND, RedisRecommendRules),
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=RedisRecommendRules,
    catalog=CatalogSource("redis-skus.yaml", RedisSkuCatalog),
)
