"""Azure Cache for Redis downsizing rule and SKU catalog model. Pure functions."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, RootModel

from models import ColdFinding, Confidence, Recommendation

CATALOG_FILE = "config/redis-skus.yaml"

# Every Premium reason ends with this: dropping to Standard loses clustering, persistence, and
# VNet injection, which this rule does not check.
_PREMIUM_NOTE = " Premium→Standard not evaluated (clustering, persistence, VNet)."


class RedisSku(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: str
    memory_gb: float


class RedisSkuCatalog(RootModel[dict[str, RedisSku]]):
    """config/redis-skus.yaml: cache sizes (`C0` … `C6`, `P1` … `P5`) with their memory."""

    def get(self, size: str) -> RedisSku | None:
        for name, spec in self.root.items():
            if name.lower() == size.lower():
                return spec
        return None

    def family_members(self, family: str) -> list[tuple[str, RedisSku]]:
        members = [(n, s) for n, s in self.root.items() if s.family == family]
        return sorted(members, key=lambda item: item[1].memory_gb)


class RedisRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_capacity: int = 0
    headroom: float = 1.3


def capacity_index(size: str) -> int:
    """The number in a size name (`C1` → 1, `P3` → 3); -1 when there is none."""
    match = re.search(r"\d+", size)
    return int(match.group()) if match else -1


def _notes(tier: str, shards: int) -> str:
    """Caveats appended to every reason: per-shard sizing, then the Premium tier boundary."""
    notes = ""
    if shards > 1:
        notes += f" Cache has {shards} shards; the size applies per shard."
    if tier.lower() == "premium":
        notes += _PREMIUM_NOTE
    return notes


def recommend_redis(
    finding: ColdFinding, catalog: RedisSkuCatalog, rules: RedisRecommendRules
) -> Recommendation:
    resource = finding.resource
    tier = str(resource.prop("tier", "") or "")
    size = str(resource.prop("size", "") or "")
    shards = int(resource.prop("shards", 0) or 0)
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
        parts.append(f"P{mem.percentile} used memory {mem.value:g}% is below {mem.threshold:g}%.")
        confidence = "medium"
    else:
        parts.append("Memory not evaluated (no usedmemorypercentage data).")
        confidence = "low"
    evidence = " ".join(parts)
    notes = _notes(tier, shards)

    current = catalog.get(size)
    if current is None:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=(
                f"{evidence} Size {size or 'unknown'} not in {CATALOG_FILE}; cannot "
                f"recommend.{notes}"
            ),
        )
    # Same family, smaller memory, at or above the configured floor; the largest is the candidate.
    candidates = [
        (name, spec)
        for name, spec in catalog.family_members(current.family)
        if spec.memory_gb < current.memory_gb and capacity_index(name) >= rules.min_capacity
    ]
    if not candidates:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence=confidence,
            reason=(
                f"{evidence} {resource.sku} is already the smallest allowed size in family "
                f"{current.family} (min_capacity={rules.min_capacity}).{notes}"
            ),
        )
    target_size, target = candidates[-1]
    fit = ""
    if mem is not None:
        needed_gb = current.memory_gb * mem.value / 100 * rules.headroom
        if needed_gb > target.memory_gb:
            return Recommendation(
                finding=finding,
                target_sku=None,
                confidence=confidence,
                reason=(
                    f"{evidence} No smaller size in family {current.family} fits the peak "
                    f"memory: {needed_gb:g} GB needed with {rules.headroom:g}x headroom, "
                    f"{target_size} has {target.memory_gb:g} GB.{notes}"
                ),
            )
        fit = f" {target_size} fits the peak memory with {rules.headroom:g}x headroom."
    return Recommendation(
        finding=finding,
        target_sku=f"{tier} {target_size}".strip(),
        confidence=confidence,
        reason=f"{evidence} Next smaller size in family {current.family}.{fit}{notes}",
    )
