"""Event Hubs namespaces: Resource Graph query, parser, capacity model, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.eventhub import EventHubRecommendRules, recommend_eventhub, unit_label
from resource_types.registry import ResourceTypeSpec, parse_tags

KIND = "eventhub"
ARM_TYPE = "microsoft.eventhub/namespaces"
DEDICATED = "dedicated"

# Azure publishes 1 MB/s ingress per TU; PU ingress is quoted as 5-10 MB/s and the conservative
# end is used. Azure quotes those MB/s decimally, so a MB here is 1_000_000 bytes, not a MiB:
# using 1024*1024 would understate utilization by ~4.8 % against the FinOps threshold.
UNIT_MBPS = {"basic": 1, "standard": 1, "premium": 5}
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
    props: dict[str, Any] = {
        "tier": tier,
        "capacity": capacity,
        "auto_inflate": bool(row.get("autoInflate") or False),
        "max_tu": int(row.get("maxTu") or 0),
    }
    unit_mbps = UNIT_MBPS.get(tier.lower())
    if unit_mbps is not None and capacity > 0:
        props["capacity_bytes_per_second"] = capacity * unit_mbps * BYTES_PER_MB
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=_sku(tier, capacity),
        tags=parse_tags(row),
        props=props,
    )


def finops_skip(resource: Resource) -> Skip | None:
    if str(resource.prop("tier", "")).lower() == DEDICATED:
        return Skip(resource.id, "no_capacity_model")
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for(KIND)
    assert isinstance(rules, EventHubRecommendRules)
    return recommend_eventhub(finding, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=EventHubRecommendRules,
    priced=False,
)
