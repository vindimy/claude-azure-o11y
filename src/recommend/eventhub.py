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


def recommend_eventhub(finding: ColdFinding, rules: EventHubRecommendRules) -> Recommendation:
    resource = finding.resource
    tier = str(resource.prop("tier") or "")
    capacity = int(resource.prop("capacity") or 0)
    unit = "PU" if tier == "Premium" else "TU"
    evidence = (
        f"P95 ingress {finding.observed:g}% of {capacity} {unit} over {finding.lookback_days}d "
        f"is below {finding.threshold:g}%."
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
