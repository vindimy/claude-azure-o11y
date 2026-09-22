"""Service Bus namespaces: Resource Graph query, parser, active check, recommender binding.

Same shape as Event Hubs. Ops watches throttling, server errors, dead-letter depth, and the
Premium CPU/memory metrics; FinOps sizes Premium messaging units. Basic and Standard are
multi-tenant with no capacity of their own, so FinOps skips them (`no_capacity_model`).
"""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.servicebus import ServiceBusRecommendRules, recommend_servicebus
from resource_types.registry import ResourceTypeSpec, base_resource, require_state

KIND = "servicebus"
ARM_TYPE = "microsoft.servicebus/namespaces"
PREMIUM = "premium"
ACTIVE = "active"

QUERY = """
resources
| where type =~ 'microsoft.servicebus/namespaces'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.name), capacity = toint(sku.capacity),
          partitions = toint(properties.premiumMessagingPartitions),
          status = tostring(properties.status)
| order by id asc
"""


def _sku(tier: str, capacity: int) -> str:
    """`Sku` column: Premium bills messaging units; Basic/Standard have no size."""
    if tier.lower() == PREMIUM:
        return f"{tier} {capacity} MU"
    return tier


def parse(row: dict[str, Any]) -> Resource:
    tier = str(row.get("tier") or "")
    capacity = int(row.get("capacity") or 0)
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=_sku(tier, capacity),
        props={
            "tier": tier,
            "capacity": capacity,
            "partitions": int(row.get("partitions") or 0),
            "status": str(row.get("status") or ""),
        },
    )


active = require_state("status", ACTIVE, "not_active")


def finops_skip(resource: Resource) -> Skip | None:
    tier = str(resource.prop("tier", ""))
    if tier.lower() != PREMIUM:
        return Skip(resource.id, "no_capacity_model", tier)
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_servicebus(finding, config.rules_for(KIND, ServiceBusRecommendRules))


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=ServiceBusRecommendRules,
)
