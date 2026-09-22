"""Storage account access-tier rule. Pure function; contract in docs/agents/recommendations.md.

A Hot-tier account whose P95 hourly transaction count is below the cold threshold is accessed too
rarely to pay Hot storage prices; the target is the same redundancy SKU in the configured colder
tier. Used capacity is the sizing input: below `min_gib` the saving is not worth the tier change.
The metrics are account-level, so every recommendation is low confidence.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Recommendation

GIB = float(2**30)


class StorageRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_gib: float = 100
    target_tier: str = "Cool"


def recommend_storage(finding: ColdFinding, rules: StorageRecommendRules) -> Recommendation:
    obs = finding.observation("transactions")
    assert obs is not None, "storage finding without a transactions observation"
    evidence = (
        f"P{obs.percentile} hourly transactions {obs.value:g} over {finding.lookback_days}d is "
        f"below {obs.threshold:g}."
    )
    used = finding.inputs.get("used_capacity")
    if used is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=f"{evidence} Used capacity unknown; cannot size.",
        )
    used_gib = used / GIB
    if used_gib < rules.min_gib:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=(
                f"{evidence} {used_gib:.0f} GiB is below min_gib={rules.min_gib:g}; tiering saves "
                "too little."
            ),
        )
    sku_name = str(finding.resource.prop("sku_name", ""))
    access_tier = str(finding.resource.prop("access_tier", ""))
    return Recommendation(
        finding=finding,
        target_sku=f"{sku_name} {rules.target_tier}",
        confidence="low",
        reason=(
            f"{evidence} {used_gib:.0f} GiB in the {access_tier} tier: set the default access tier "
            f"to {rules.target_tier} or add a lifecycle rule. Account-level; verify per container "
            "and check early-deletion and retrieval charges."
        ),
    )
