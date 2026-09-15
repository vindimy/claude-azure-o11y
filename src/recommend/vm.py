"""VM downsizing rule. Pure function; contract documented in CLAUDE.md."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from config.models import VmRecommendRules, VmSkuCatalog
from models import ColdFinding, Recommendation


def recommend_vm(
    finding: ColdFinding, catalog: VmSkuCatalog, rules: VmRecommendRules
) -> Recommendation:
    size = finding.resource.vm_size
    evidence = (
        f"P95 CPU {finding.observed_p95:g}% over {finding.lookback_days}d is below "
        f"{finding.threshold:g}%. Memory not evaluated (MVP)."
    )
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
            confidence="medium",
            reason=(
                f"{evidence} {size} is already smallest allowed size in family "
                f"{current.family} (min_vcpu={rules.min_vcpu})."
            ),
        )
    target_name, _ = candidates[-1]
    return Recommendation(
        finding=finding,
        target_sku=target_name,
        confidence="medium",
        reason=f"{evidence} Next smaller size in family {current.family}.",
    )


def with_pricing(
    rec: Recommendation, current: Decimal | None, projected: Decimal | None
) -> Recommendation:
    return replace(rec, current_monthly=current, projected_monthly=projected)
