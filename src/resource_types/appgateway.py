"""Application Gateways: Resource Graph query, parser, capacity model, recommender binding.

The FinOps metric is consumed capacity units as a share of the units a v2 gateway reserves
(instances × CU per instance). The CU rate is a `recommend:` knob the pure parser cannot see, so
`enrich` attaches the reserved units once the pipeline has the type's rules.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pydantic import BaseModel

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.appgateway import AppGatewayRecommendRules, recommend_appgateway
from resource_types.registry import ResourceTypeSpec, base_resource, require_state

KIND = "appgateway"
ARM_TYPE = "microsoft.network/applicationgateways"
V2_SUFFIX = "_v2"

# The SKU lives under properties.sku, not the top-level sku column other types project.
QUERY = """
resources
| where type =~ 'microsoft.network/applicationgateways'
| project id, name, subscriptionId, resourceGroup, location, tags,
          skuName = tostring(properties.sku.name), tier = tostring(properties.sku.tier),
          capacity = toint(properties.sku.capacity),
          autoscale = isnotnull(properties.autoscaleConfiguration),
          minCapacity = toint(properties.autoscaleConfiguration.minCapacity),
          maxCapacity = toint(properties.autoscaleConfiguration.maxCapacity),
          state = tostring(properties.operationalState)
| order by id asc
"""


def _sku(
    sku_name: str, autoscale: bool, capacity: int, min_capacity: int, max_capacity: int
) -> str:
    """`Sku` column: `Standard_v2 autoscale 2-10`, `WAF_v2 x3`, `Standard_Medium x2`."""
    if not sku_name:
        return ""
    if autoscale:
        return f"{sku_name} autoscale {min_capacity}-{max_capacity}"
    return f"{sku_name} x{capacity}"


def parse(row: dict[str, Any]) -> Resource:
    sku_name = str(row.get("skuName") or "")
    tier = str(row.get("tier") or "")
    autoscale = bool(row.get("autoscale") or False)
    capacity = int(row.get("capacity") or 0)
    min_capacity = int(row.get("minCapacity") or 0)
    max_capacity = int(row.get("maxCapacity") or 0)
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=_sku(sku_name, autoscale, capacity, min_capacity, max_capacity),
        props={
            "sku_name": sku_name,
            "tier": tier,
            "v2": tier.lower().endswith(V2_SUFFIX),
            "autoscale": autoscale,
            "capacity": capacity,
            "min_capacity": min_capacity,
            "max_capacity": max_capacity,
            "state": str(row.get("state") or ""),
        },
    )


def enrich(resource: Resource, rules: BaseModel) -> Resource:
    """Attach `reserved_capacity_units` = instances × CU per instance for v2 gateways.

    The instance count is the autoscale minimum or the fixed capacity. v1 gateways and autoscale
    gateways with a minimum of 0 get none: FinOps has no capacity model for them, so the derived
    capacity metric is not evaluated (`metrics.derive.resolve_series`).
    """
    assert isinstance(rules, AppGatewayRecommendRules)
    if not resource.prop("v2", False):
        return resource
    instances = int(
        resource.prop("min_capacity" if resource.prop("autoscale") else "capacity", 0) or 0
    )
    reserved = instances * rules.cu_per_instance
    if reserved <= 0:
        return resource
    return replace(resource, props={**resource.props, "reserved_capacity_units": reserved})


active = require_state("state", "Running", "not_running")


def finops_skip(resource: Resource) -> Skip | None:
    if not resource.prop("v2", False):
        return Skip(resource.id, "no_capacity_model", str(resource.prop("tier") or "v1"))
    if not resource.prop("reserved_capacity_units"):
        return Skip(resource.id, "no_capacity_model", "autoscale minimum 0")
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_appgateway(finding, config.rules_for(KIND, AppGatewayRecommendRules))


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    enrich=enrich,
    active=active,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=AppGatewayRecommendRules,
    priced=False,
)
