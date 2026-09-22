"""PostgreSQL Flexible Server downsizing rule. Pure function; mirrors recommend/vm.py.

Pricing is not implemented for PostgreSQL (no PricingPort lookup wired in
resource_types/postgres.py), so every reason ends with a note saying so, and `with_pricing` is
never applied to this type's rows.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import VmSkuCatalog
from models import ColdFinding, Confidence, Recommendation

_PRICING_NOTE = "Pricing not implemented for PostgreSQL."


class PostgresRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcpu: int = 2


def recommend_postgres(
    finding: ColdFinding, catalog: VmSkuCatalog, rules: PostgresRecommendRules
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
        parts.append(f"P{mem.percentile} memory {mem.value:g}% is below {mem.threshold:g}%.")
        confidence = "medium"
    else:
        parts.append("Memory not evaluated (no memory_percent data).")
        confidence = "low"
    evidence = " ".join(parts)
    current = catalog.get(size)
    if current is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=(
                f"{evidence} SKU {size} not in config/postgres-skus.yaml; cannot recommend. "
                f"{_PRICING_NOTE}"
            ),
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
                f"{current.family} (min_vcpu={rules.min_vcpu}). {_PRICING_NOTE}"
            ),
        )
    target_name, _ = candidates[-1]
    return Recommendation(
        finding=finding,
        target_sku=target_name,
        confidence=confidence,
        reason=f"{evidence} Next smaller size in family {current.family}. {_PRICING_NOTE}",
    )
