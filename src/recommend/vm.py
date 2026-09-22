"""VM downsizing rule. Pure function; contract documented in docs/agents/recommendations.md."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import FamilySkuCatalog
from models import ColdFinding, Confidence, Recommendation
from recommend.ladder import downsize_in_family


class VmRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcpu: int = 1


def recommend_vm(
    finding: ColdFinding, catalog: FamilySkuCatalog, rules: VmRecommendRules
) -> Recommendation:
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
    return downsize_in_family(
        finding,
        catalog,
        catalog_file="config/vm-skus.yaml",
        min_vcpu=rules.min_vcpu,
        evidence=" ".join(parts),
        confidence=confidence,
    )
