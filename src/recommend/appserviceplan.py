"""App Service Plan downsizing rule. Pure function; contract in docs/agents/recommendations.md.

Two levers, tried in order: fewer instances of the same SKU (the cheap, reversible change), then
one step down the SKU family at the same instance count. A plan with no apps is a delete.
"""

from __future__ import annotations

from math import ceil

from pydantic import BaseModel, ConfigDict

from config.models import FamilySkuCatalog
from models import ColdFinding, Confidence, Recommendation
from recommend.ladder import next_smaller_in_family

CATALOG_FILE = "config/appservice-skus.yaml"
DELETE = "delete"


class AppServicePlanRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_instances: int = 1
    min_vcpu: int = 1
    headroom: float = 1.3


def recommend_appserviceplan(
    finding: ColdFinding, catalog: FamilySkuCatalog, rules: AppServicePlanRecommendRules
) -> Recommendation:
    resource = finding.resource
    sku_name = str(resource.prop("sku_name") or "")
    instances = int(resource.prop("instances") or 0)
    sites = int(resource.prop("sites") or 0)
    elastic = bool(resource.prop("elastic_scale") or False)

    cpu = finding.observation("cpu")
    mem = finding.observation("memory")
    parts: list[str] = []
    if cpu is not None:
        parts.append(
            f"P{cpu.percentile} CPU {cpu.value:g}% over {finding.lookback_days}d is below "
            f"{cpu.threshold:g}%."
        )
    confidence: Confidence
    if mem is not None:
        parts.append(f"P{mem.percentile} memory {mem.value:g}% is below {mem.threshold:g}%.")
        confidence = "medium"
    else:
        parts.append("Memory not evaluated (no MemoryPercentage data).")
        confidence = "low"
    evidence = " ".join(parts)
    # Both metrics are shares of the whole plan, so the busier one sizes the target.
    peak = max([o.value for o in (cpu, mem) if o is not None] or [finding.observed])

    if sites == 0:
        return Recommendation(
            finding=finding,
            target_sku=DELETE,
            confidence=confidence,
            reason=f"{evidence} The plan hosts no apps ({sites} sites); delete it.",
        )

    # Lever 1: scale in. Instances form a contiguous ladder, so the smallest count that covers the
    # peak plus headroom is a plain ceiling. Elastic plans manage their own count.
    if not elastic and instances > rules.min_instances:
        n = max(rules.min_instances, ceil(instances * peak / 100 * rules.headroom))
        if n < instances:
            return Recommendation(
                finding=finding,
                target_sku=f"{sku_name} x{n}",
                confidence=confidence,
                reason=(
                    f"{evidence} {n} instances cover the peak with {rules.headroom:g}x headroom."
                ),
            )

    # Lever 2: one step down the SKU family at the same instance count.
    lever = "Elastic scale manages the instance count" if elastic else "Instance count is at floor"
    current = catalog.get(sku_name)
    if current is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} SKU {sku_name} not in {CATALOG_FILE}; cannot recommend.",
        )
    smaller = next_smaller_in_family(catalog, current, rules.min_vcpu)
    if smaller is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence=confidence,
            reason=(
                f"{evidence} {lever}; {sku_name} is already smallest allowed size in family "
                f"{current.family} (min_vcpu={rules.min_vcpu})."
            ),
        )
    target = catalog.get(smaller)
    assert target is not None
    projected = peak * rules.headroom * current.vcpu / target.vcpu
    if projected > 100:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence=confidence,
            reason=(
                f"{evidence} {lever}; {smaller} would not fit the peak with "
                f"{rules.headroom:g}x headroom ({projected:.0f}% of {target.vcpu} vCPU)."
            ),
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{smaller} x{instances}",
        confidence=confidence,
        reason=(
            f"{evidence} {lever}; {smaller} x{instances} covers the peak with "
            f"{rules.headroom:g}x headroom."
        ),
    )
