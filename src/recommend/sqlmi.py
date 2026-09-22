"""Azure SQL Managed Instance downsizing rule. Pure function; no pricing (SQL is unpriced, MVP)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import SqlSkuCatalog
from models import ColdFinding, Recommendation
from recommend.ladder import fit_up

_PRICING_NOTE = " Pricing not implemented for SQL."


class SqlMiRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcores: int = 4
    headroom: float = 1.3


def recommend_sqlmi(
    finding: ColdFinding, catalog: SqlSkuCatalog, rules: SqlMiRecommendRules
) -> Recommendation:
    resource = finding.resource
    evidence = (
        f"P{finding.percentile} CPU {finding.observed:g}% over {finding.lookback_days}d is below "
        f"{finding.threshold:g}%."
    )
    sku_name = str(resource.prop("sku_name", "") or "")
    vcores = int(resource.prop("vcores", 0) or 0)
    ladder = catalog.vcore.get("managed_instance", [])
    if not ladder or vcores == 0:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} vCore ladder or capacity unknown; cannot recommend.{_PRICING_NOTE}",
        )
    needed = vcores * finding.observed / 100 * rules.headroom
    target = fit_up(ladder, needed, rules.min_vcores, vcores)
    if target is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="medium",
            reason=(
                f"{evidence} {resource.sku} is already at min_vcores={rules.min_vcores}."
                f"{_PRICING_NOTE}"
            ),
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{sku_name} {target} vCores",
        confidence="medium",
        reason=(
            f"{evidence} {target} vCores cover P{finding.percentile} with "
            f"{rules.headroom:g}x headroom.{_PRICING_NOTE}"
        ),
    )
