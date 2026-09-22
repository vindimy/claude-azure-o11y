"""Elastic pool downsizing rule. Pure function; contract in docs/agents/recommendations.md."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import SqlSkuCatalog
from models import ColdFinding, Recommendation
from recommend.ladder import fit_down


class SqlPoolRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcores: int = 2
    headroom: float = 1.3


def recommend_sqlpool(
    finding: ColdFinding, catalog: SqlSkuCatalog, rules: SqlPoolRecommendRules
) -> Recommendation:
    resource = finding.resource
    tier = str(resource.prop("tier", ""))
    sku_name = str(resource.prop("sku_name", ""))
    capacity = int(resource.prop("capacity", 0) or 0)
    purchasing_model = str(resource.prop("purchasing_model", ""))
    evidence = finding.evidence(finding.metric.upper())

    if purchasing_model == "vcore":
        ladder = catalog.vcore.get("pool", [])
        if not ladder or capacity <= 0:
            return Recommendation(
                finding=finding,
                target_sku=None,
                confidence="low",
                reason=f"{evidence} vCore ladder or capacity unknown; cannot recommend.",
            )
        target = fit_down(ladder, capacity, finding.observed, rules.headroom, rules.min_vcores)
        if target is None:
            return Recommendation(
                finding=finding,
                target_sku=None,
                confidence="medium",
                reason=f"{evidence} already at min_vcores={rules.min_vcores}.",
            )
        return Recommendation(
            finding=finding,
            target_sku=f"{sku_name} {target}",
            confidence="medium",
            reason=(
                f"{evidence} {target} vCores cover P{finding.percentile} with "
                f"{rules.headroom:g}x headroom."
            ),
        )

    ladder = catalog.pool_edtu.get(tier, [])
    if not ladder:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} {tier} not in config/sql-skus.yaml; cannot recommend.",
        )
    target = fit_down(ladder, capacity, finding.observed, rules.headroom)
    if target is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="medium",
            reason=f"{evidence} {sku_name} is already the smallest {tier} pool size.",
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{sku_name} {target}",
        confidence="medium",
        reason=(
            f"{evidence} {target} eDTU covers P{finding.percentile} with "
            f"{rules.headroom:g}x headroom."
        ),
    )
