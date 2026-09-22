"""Ordered-size helpers shared by the recommenders. Pure.

`fit_up` / `fit_down` walk an integer ladder (DTUs, eDTUs, vCores); `next_smaller_in_family` and
`downsize_in_family` walk a SKU family from a `FamilySkuCatalog` (VMs, PostgreSQL).
"""

from __future__ import annotations

from collections.abc import Sequence

from config.models import FamilySku, FamilySkuCatalog
from models import ColdFinding, Confidence, Recommendation


def fit_up(ladder: Sequence[int], needed: float, floor: int, below: int) -> int | None:
    """Smallest ladder size that covers `needed` (and `floor`) while staying below `below`."""
    candidates = [s for s in ladder if s >= max(needed, floor) and s < below]
    return min(candidates) if candidates else None


def fit_down(
    ladder: Sequence[int], current: int, observed_percent: float, headroom: float, floor: int = 0
) -> int | None:
    """Smallest ladder size below `current` that still covers the observed share plus headroom.

    `needed = current × observed_percent / 100 × headroom`; None when nothing below `current`
    (and at or above `floor`) covers it.
    """
    return fit_up(ladder, current * observed_percent / 100 * headroom, floor, current)


def next_smaller_in_family(
    catalog: FamilySkuCatalog, current: FamilySku, min_vcpu: int
) -> str | None:
    """Largest same-family SKU with fewer vCPUs than `current`, never below `min_vcpu`, by name."""
    candidates = [
        name
        for name, spec in catalog.family_members(current.family)
        if min_vcpu <= spec.vcpu < current.vcpu
    ]
    return candidates[-1] if candidates else None


def downsize_in_family(
    finding: ColdFinding,
    catalog: FamilySkuCatalog,
    *,
    catalog_file: str,
    min_vcpu: int,
    evidence: str,
    confidence: Confidence,
) -> Recommendation:
    """The shared family rule: one step down the family ladder, or say why there is no step.

    `evidence` and `confidence` are the type's own reading of the cold metrics; this adds the
    target (or the reason there is none) and drops confidence to `low` when the SKU is unknown.
    """
    size = finding.resource.sku
    current = catalog.get(size)
    if current is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} SKU {size} not in {catalog_file}; cannot recommend.",
        )
    target = next_smaller_in_family(catalog, current, min_vcpu)
    if target is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence=confidence,
            reason=(
                f"{evidence} {size} is already smallest allowed size in family "
                f"{current.family} (min_vcpu={min_vcpu})."
            ),
        )
    return Recommendation(
        finding=finding,
        target_sku=target,
        confidence=confidence,
        reason=f"{evidence} Next smaller size in family {current.family}.",
    )
