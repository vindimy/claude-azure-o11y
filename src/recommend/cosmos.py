"""Cosmos DB throughput rule. Pure function; contract in docs/agents/recommendations.md.

Normalized RU consumption is the peak use of the provisioned (or autoscale maximum) throughput, so
the sized target is that share of the current throughput plus headroom, rounded up to 100 RU/s. The
metric is account-level, so every recommendation is low confidence.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Recommendation


class CosmosRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_ru: int = 400
    headroom: float = 1.3
    autoscale_ratio: float = 3.0


def recommend_cosmos(finding: ColdFinding, rules: CosmosRecommendRules) -> Recommendation:
    ru = finding.observation("ru")
    assert ru is not None, "cosmos finding without a ru observation"
    provisioned = finding.inputs.get("provisioned")
    autoscale_max = finding.inputs.get("autoscale_max")
    evidence = (
        f"P{ru.percentile} normalized RU {ru.value:g}% over {finding.lookback_days}d is below "
        f"{ru.threshold:g}% (median {ru.median:g}%)."
    )
    verify = " Account-level value; verify per container. Pricing not implemented for Cosmos DB."
    base = autoscale_max or provisioned
    if not base:
        return Recommendation(
            finding,
            None,
            "low",
            f"{evidence} No provisioned throughput metric; cannot size.{verify}",
        )
    target = max(rules.min_ru, math.ceil(base * ru.value / 100 * rules.headroom / 100) * 100)
    if target >= base:
        return Recommendation(
            finding,
            None,
            "low",
            f"{evidence} Sized target {target} RU/s is not below current {base:g} RU/s.{verify}",
        )
    bursty = ru.median > 0 and ru.value / ru.median >= rules.autoscale_ratio
    if autoscale_max:
        return Recommendation(
            finding,
            f"autoscale {target} RU/s max",
            "low",
            f"{evidence} Lower the autoscale max from {base:g} RU/s.{verify}",
        )
    if bursty:
        return Recommendation(
            finding,
            f"autoscale {target} RU/s max",
            "low",
            f"{evidence} P95/median ratio {ru.value / ru.median:.1f} suggests autoscale.{verify}",
        )
    return Recommendation(
        finding,
        f"{target} RU/s",
        "low",
        f"{evidence} Lower provisioned throughput from {base:g} RU/s.{verify}",
    )
