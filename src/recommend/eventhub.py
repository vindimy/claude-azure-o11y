"""Event Hubs downsizing rule. Pure function; contract in docs/agents/recommendations.md."""

from __future__ import annotations

from math import ceil

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Recommendation

# Every reason ends with this: Standard<->Basic needs capture/consumer-group/retention checks we
# do not perform, and Event Hubs has no pricing support yet (recommend/pricing.py is VM-only).
_NOTE = (
    " Standard→Basic not evaluated (needs capture/consumer-group/retention checks). "
    "Pricing not implemented for Event Hubs."
)


class EventHubRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headroom: float = 1.3


def unit_label(tier: str) -> str:
    """Capacity unit of a namespace tier: Premium bills processing units, the rest throughput units.

    The single source of the label; `resource_types/eventhub.py` spells `Sku` with it and this
    module spells `RecommendedSku`. Case-insensitive: Resource Graph's casing is not guaranteed.
    """
    return "PU" if tier.lower() == "premium" else "TU"


def recommend_eventhub(finding: ColdFinding, rules: EventHubRecommendRules) -> Recommendation:
    resource = finding.resource
    tier = str(resource.prop("tier") or "")
    capacity = int(resource.prop("capacity") or 0)
    unit = unit_label(tier)
    evidence = (
        f"P{finding.percentile} ingress {finding.observed:g}% of {capacity} {unit} over "
        f"{finding.lookback_days}d is below {finding.threshold:g}%."
    )
    target = max(1, ceil(capacity * finding.observed / 100 * rules.headroom))
    if target >= capacity:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="medium",
            reason=(
                f"{evidence} {tier} {capacity} {unit} is already at the smallest size that "
                f"covers the ingress.{_NOTE}"
            ),
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{tier} {target} {unit}",
        confidence="medium",
        reason=(
            f"{evidence} {tier} {target} {unit} covers the observed ingress with "
            f"{rules.headroom:g}x headroom.{_NOTE}"
        ),
    )
