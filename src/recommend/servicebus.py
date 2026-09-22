"""Service Bus Premium messaging-unit rule. Pure; contract in docs/agents/recommendations.md.

Only Premium namespaces have dedicated capacity (messaging units) and the CPU/memory metrics
that show how much of it is used; Basic and Standard are skipped before the fetch
(`no_capacity_model`). The unit ladder is a knob so a new size never needs code.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Confidence, Recommendation
from recommend.ladder import fit_down

# Premium->Standard needs VNet, message-size, and feature checks we do not perform.
_NOTE = " Premium→Standard not evaluated (VNet, message size, feature checks)."


class ServiceBusRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    headroom: float = 1.3
    min_units: int = 1
    messaging_units: list[int] = [1, 2, 4, 8, 16]


def recommend_servicebus(finding: ColdFinding, rules: ServiceBusRecommendRules) -> Recommendation:
    resource = finding.resource
    tier = str(resource.prop("tier") or "")
    capacity = int(resource.prop("capacity") or 0)
    partitions = int(resource.prop("partitions") or 0)
    cpu = finding.observation("cpu")
    mem = finding.observation("memory")
    parts: list[str] = []
    peaks: list[float] = []
    if cpu is not None:
        parts.append(
            f"P{cpu.percentile} CPU {cpu.value:g}% over {finding.lookback_days}d is below "
            f"{cpu.threshold:g}%."
        )
        peaks.append(cpu.value)
    confidence: Confidence
    if mem is not None:
        parts.append(f"P{mem.percentile} memory {mem.value:g}% is below {mem.threshold:g}%.")
        peaks.append(mem.value)
        confidence = "medium"
    else:
        parts.append("Memory not evaluated (no NamespaceMemoryUsage data).")
        confidence = "low"
    if partitions > 1:
        parts.append(f"Namespace has {partitions} messaging partitions.")
    evidence = " ".join(parts)
    if capacity <= 0:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} Capacity unknown; cannot recommend.{_NOTE}",
        )
    peak = max(peaks) if peaks else finding.observed
    target = fit_down(rules.messaging_units, capacity, peak, rules.headroom, rules.min_units)
    if target is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence=confidence,
            reason=(
                f"{evidence} {tier} {capacity} MU is already at the smallest size that covers "
                f"the peak (min_units={rules.min_units}).{_NOTE}"
            ),
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{tier} {target} MU",
        confidence=confidence,
        reason=(
            f"{evidence} {tier} {target} MU covers the peak with {rules.headroom:g}x headroom."
            f"{_NOTE}"
        ),
    )
