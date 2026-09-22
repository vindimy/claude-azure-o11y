"""Azure SQL Managed Instance downsizing rule. Pure function."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import SqlSkuCatalog
from models import ColdFinding, Recommendation
from recommend.ladder import fit_down


class SqlMiRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcores: int = 4
    headroom: float = 1.3


def recommend_sqlmi(
    finding: ColdFinding, catalog: SqlSkuCatalog, rules: SqlMiRecommendRules
) -> Recommendation:
    resource = finding.resource
    evidence = finding.evidence("CPU")
    sku_name = str(resource.prop("sku_name", "") or "")
    vcores = int(resource.prop("vcores", 0) or 0)
    ladder = catalog.vcore.get("managed_instance", [])
    if not ladder or vcores == 0:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} vCore ladder or capacity unknown; cannot recommend.",
        )
    target = fit_down(ladder, vcores, finding.observed, rules.headroom, rules.min_vcores)
    if target is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="medium",
            reason=f"{evidence} {resource.sku} is already at min_vcores={rules.min_vcores}.",
        )
    return Recommendation(
        finding=finding,
        target_sku=f"{sku_name} {target} vCores",
        confidence="medium",
        reason=(
            f"{evidence} {target} vCores cover P{finding.percentile} with "
            f"{rules.headroom:g}x headroom."
        ),
    )
