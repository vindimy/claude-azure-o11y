"""Azure SQL Database downsizing rule. Pure function; contract in docs/agents/recommendations.md.

A cold database drops to the smallest service objective (DTU) or vCore size that still covers its
observed peak with headroom. SQL is not priced yet, so every reason says so.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import SqlSkuCatalog
from models import ColdFinding, Recommendation
from recommend.ladder import fit_up

NOTE = " Pricing not implemented for SQL."


class SqlDbRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcores: int = 2
    headroom: float = 1.3


def recommend_sqldb(
    finding: ColdFinding, catalog: SqlSkuCatalog, rules: SqlDbRecommendRules
) -> Recommendation:
    r = finding.resource
    evidence = (
        f"P{finding.percentile} {finding.metric.upper()} {finding.observed:g}% over "
        f"{finding.lookback_days}d is below {finding.threshold:g}%."
    )
    tier = str(r.prop("tier", ""))
    sku_name = r.sku
    capacity = int(r.prop("capacity", 0) or 0)

    if r.prop("purchasing_model") == "dtu":
        objectives = catalog.dtu.get(tier)
        if not objectives or sku_name not in objectives:
            return Recommendation(
                finding=finding,
                target_sku=None,
                confidence="low",
                reason=(
                    f"{evidence} {sku_name} not in config/sql-skus.yaml; cannot recommend.{NOTE}"
                ),
            )
        current = objectives[sku_name]
        needed = current * finding.observed / 100 * rules.headroom
        target = fit_up(sorted(objectives.values()), needed, 0, current)
        if target is None:
            return Recommendation(
                finding=finding,
                target_sku=None,
                confidence="medium",
                reason=f"{evidence} {sku_name} is already the smallest {tier} objective.{NOTE}",
            )
        name = next(n for n, dtu in objectives.items() if dtu == target)
        return Recommendation(
            finding=finding,
            target_sku=name,
            confidence="medium",
            reason=(
                f"{evidence} {target} DTU covers P{finding.percentile} with "
                f"{rules.headroom:g}x headroom.{NOTE}"
            ),
        )

    ladder = catalog.vcore.get("database", [])
    if not ladder or capacity == 0:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} vCore ladder or capacity unknown; cannot recommend.{NOTE}",
        )
    needed = capacity * finding.observed / 100 * rules.headroom
    target = fit_up(ladder, needed, rules.min_vcores, capacity)
    if target is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="medium",
            reason=f"{evidence} {sku_name} is already at min_vcores={rules.min_vcores}.{NOTE}",
        )
    return Recommendation(
        finding=finding,
        target_sku="_".join([*sku_name.split("_")[:-1], str(target)]),
        confidence="medium",
        reason=(
            f"{evidence} {target} vCores cover P{finding.percentile} with "
            f"{rules.headroom:g}x headroom.{NOTE}"
        ),
    )
