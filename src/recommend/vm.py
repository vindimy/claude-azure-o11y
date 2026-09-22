"""VM downsizing rule. Pure function; contract documented in docs/agents/recommendations.md."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import VmSkuCatalog
from models import ColdFinding, Confidence, Recommendation


class VmRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcpu: int = 1


def recommend_vm(
    finding: ColdFinding, catalog: VmSkuCatalog, rules: VmRecommendRules
) -> Recommendation:
    size = finding.resource.sku
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
        parts.append(
            f"P{mem.percentile} available memory {mem.value:g}% is above {mem.threshold:g}% "
            f"(peak use {100 - mem.value:g}%)."
        )
        confidence = "medium"
    else:
        parts.append("Memory not evaluated (no Available Memory Percentage data).")
        confidence = "low"
    evidence = " ".join(parts)
    current = catalog.get(size)
    if current is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} SKU {size} not in config/vm-skus.yaml; cannot recommend.",
        )
    candidates = [
        (name, spec)
        for name, spec in catalog.family_members(current.family)
        if spec.vcpu < current.vcpu and spec.vcpu >= rules.min_vcpu
    ]
    if not candidates:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence=confidence,
            reason=(
                f"{evidence} {size} is already smallest allowed size in family "
                f"{current.family} (min_vcpu={rules.min_vcpu})."
            ),
        )
    target_name, _ = candidates[-1]
    return Recommendation(
        finding=finding,
        target_sku=target_name,
        confidence=confidence,
        reason=f"{evidence} Next smaller size in family {current.family}.",
    )
