"""PostgreSQL Flexible Server downsizing rule. Pure function; the VM family rule with the
server's own memory reading (memory_percent is used memory, not available memory)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from config.models import FamilySkuCatalog
from models import ColdFinding, Confidence, Recommendation
from recommend.ladder import downsize_in_family


class PostgresRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_vcpu: int = 2


def recommend_postgres(
    finding: ColdFinding, catalog: FamilySkuCatalog, rules: PostgresRecommendRules
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
        parts.append(f"P{mem.percentile} memory {mem.value:g}% is below {mem.threshold:g}%.")
        confidence = "medium"
    else:
        parts.append("Memory not evaluated (no memory_percent data).")
        confidence = "low"
    return downsize_in_family(
        finding,
        catalog,
        catalog_file="config/postgres-skus.yaml",
        min_vcpu=rules.min_vcpu,
        evidence=" ".join(parts),
        confidence=confidence,
    )
