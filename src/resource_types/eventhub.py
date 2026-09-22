"""Event Hubs namespaces: Resource Graph query, parser, capacity model, recommender binding.

The FinOps metric is ingress as a share of the namespace's capacity in bytes per second. That
capacity depends on the configured MB/s per TU or PU (`recommend:` knobs), which the pure parser
cannot see, so `enrich` attaches it once the pipeline has the type's rules.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.eventhub import EventHubRecommendRules, recommend_eventhub, unit_label
from resource_types.registry import ResourceTypeSpec, base_resource

KIND = "eventhub"
ARM_TYPE = "microsoft.eventhub/namespaces"
DEDICATED = "dedicated"

# Azure quotes unit throughput in decimal MB/s, so a MB here is 1_000_000 bytes, not a MiB: using
# 1024*1024 would understate utilization by ~4.8 % against the FinOps threshold.
BYTES_PER_MB = 1_000_000

QUERY = """
resources
| where type =~ 'microsoft.eventhub/namespaces'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.name), capacity = toint(sku.capacity),
          autoInflate = tobool(properties.isAutoInflateEnabled),
          maxTu = toint(properties.maximumThroughputUnits)
| order by id asc
"""


def _sku(tier: str, capacity: int) -> str:
    """`Sku` column for a namespace, in the tier casing Resource Graph returned."""
    if not tier:
        return ""
    if tier.lower() == DEDICATED:
        return tier
    return f"{tier} {capacity} {unit_label(tier)}"


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
            "auto_inflate": bool(row.get("autoInflate") or False),
            "max_tu": int(row.get("maxTu") or 0),
        },
    )


def enrich(resource: Resource, rules: BaseModel) -> Resource:
    """Attach `capacity_bytes_per_second` from the configured unit rates.

    Dedicated clusters and namespaces without a capacity get none: FinOps has no capacity model
    for them, so the derived ingress metric is not evaluated (`metrics.derive.resolve_series`).
    """
    assert isinstance(rules, EventHubRecommendRules)
    unit_mbps = rules.unit_mbps(str(resource.prop("tier", "")))
    capacity = int(resource.prop("capacity", 0) or 0)
    if unit_mbps is None or capacity <= 0:
        return resource
    bytes_per_second = capacity * unit_mbps * BYTES_PER_MB
    return replace(
        resource, props={**resource.props, "capacity_bytes_per_second": bytes_per_second}
    )


def finops_skip(resource: Resource) -> Skip | None:
    if str(resource.prop("tier", "")).lower() == DEDICATED:
        return Skip(resource.id, "no_capacity_model")
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_eventhub(finding, config.rules_for(KIND, EventHubRecommendRules))


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    enrich=enrich,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=EventHubRecommendRules,
)
