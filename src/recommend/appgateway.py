"""Application Gateway downsizing rule. Pure; contract in docs/agents/recommendations.md."""

from __future__ import annotations

from math import ceil

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Recommendation


class AppGatewayRecommendRules(BaseModel):
    """`recommend:` knobs. `cu_per_instance` sizes the FinOps capacity model (reserved units =
    instances × CU per instance), not just the target: Azure reserves 10 capacity units per v2
    instance."""

    model_config = ConfigDict(extra="forbid")
    cu_per_instance: int = 10
    min_instances: int = 1
    headroom: float = 1.3


def recommend_appgateway(finding: ColdFinding, rules: AppGatewayRecommendRules) -> Recommendation:
    resource = finding.resource
    sku_name = str(resource.prop("sku_name") or "")
    autoscale = bool(resource.prop("autoscale") or False)
    current = int(resource.prop("min_capacity" if autoscale else "capacity") or 0)
    reserved = int(resource.prop("reserved_capacity_units") or 0)
    evidence = (
        f"P{finding.percentile} capacity units {finding.observed:g}% of {reserved} reserved "
        f"({current} instances × {rules.cu_per_instance} CU) over {finding.lookback_days}d is "
        f"below {finding.threshold:g}%."
    )
    if current <= 0 or reserved <= 0:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} No target: capacity unknown ({sku_name or 'no SKU'}).",
        )
    # Instances form a contiguous 1..current ladder, so the smallest count whose reserved units
    # cover the consumed units plus headroom is a plain ceiling rather than a `fit_down`.
    target = max(
        rules.min_instances,
        ceil(reserved * finding.observed / 100 * rules.headroom / rules.cu_per_instance),
    )
    if target >= current:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="medium",
            reason=(
                f"{evidence} {current} instances is already at the smallest instance count that "
                f"covers the load (min_instances={rules.min_instances})."
            ),
        )
    if autoscale:
        max_capacity = int(resource.prop("max_capacity") or 0)
        return Recommendation(
            finding=finding,
            target_sku=f"{sku_name} autoscale {target}-{max_capacity}",
            confidence="medium",
            reason=f"{evidence} Lower the minimum instance count from {current} to {target}.",
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{sku_name} x{target}",
        confidence="medium",
        reason=f"{evidence} Lower the instance count from {current} to {target}.",
    )
