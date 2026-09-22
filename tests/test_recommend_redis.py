from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import load_config
from models import ColdFinding, ColdObservation, Resource
from recommend.redis import (
    RedisRecommendRules,
    RedisSku,
    RedisSkuCatalog,
    capacity_index,
    recommend_redis,
)

PREMIUM_NOTE = "Premium→Standard not evaluated (clustering, persistence, VNet)."


def finding(
    size: str,
    tier: str = "Standard",
    shards: int = 0,
    cpu: float = 4.0,
    memory: float | None = 15.0,
) -> ColdFinding:
    family = size[:1]
    cache = Resource(
        kind="redis",
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Cache/Redis/cache",
        name="cache",
        type="microsoft.cache/redis",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{tier} {size}".strip(),
        tags={},
        props={
            "tier": tier,
            "family": family,
            "capacity": capacity_index(size),
            "size": size,
            "state": "Succeeded",
            "shards": shards,
            "redis_version": "6.0",
        },
    )
    observations = [
        ColdObservation("cpu", 95, cpu, cpu, 20, True, 1.0),
    ]
    if memory is not None:
        observations.append(ColdObservation("memory", 95, memory, memory, 30, True, 1.0))
    return ColdFinding(
        resource=cache,
        metric="cpu",
        observed=cpu,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(observations),
    )


@pytest.fixture
def catalog(config_dir: Path) -> RedisSkuCatalog:
    return load_config(config_dir, "mg-x").catalog_for("redis", RedisSkuCatalog)


def test_recommends_next_smaller_size(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("C1"), catalog, RedisRecommendRules())
    assert rec.target_sku == "Standard C0"
    assert rec.confidence == "medium"
    assert "P95 CPU 4% over 14d is below 20%." in rec.reason
    assert "P95 used memory 15% is below 30%." in rec.reason
    assert "Next smaller size in family C." in rec.reason
    assert "C0 fits the peak memory with 1.3x headroom." in rec.reason
    assert "Pricing" not in rec.reason
    assert "Premium" not in rec.reason and "shard" not in rec.reason


def test_one_step_only_even_when_far_colder(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("C4", memory=1.0), catalog, RedisRecommendRules())
    assert rec.target_sku == "Standard C3"


def test_memory_does_not_fit_the_next_smaller_size(catalog: RedisSkuCatalog) -> None:
    # C2 is 2.5 GB; 25% used x 1.3 = 0.8125 GB fits C1 (1 GB). 60% does not (1.95 GB > 1 GB).
    fits = recommend_redis(finding("C2", memory=25.0), catalog, RedisRecommendRules())
    assert fits.target_sku == "Standard C1"
    rec = recommend_redis(finding("C2", memory=60.0), catalog, RedisRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "No smaller size in family C fits the peak memory:" in rec.reason
    assert "1.95 GB needed with 1.3x headroom, C1 has 1 GB." in rec.reason


def test_headroom_is_configurable(catalog: RedisSkuCatalog) -> None:
    # 2.5 GB x 35% = 0.875 GB: fits C1 with 1.0x headroom, not with 1.3x (1.1375 GB).
    tight = recommend_redis(finding("C2", memory=35.0), catalog, RedisRecommendRules(headroom=1.0))
    assert tight.target_sku == "Standard C1"
    loose = recommend_redis(finding("C2", memory=35.0), catalog, RedisRecommendRules())
    assert loose.target_sku is None


def test_already_at_min_capacity(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("C0"), catalog, RedisRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "Standard C0 is already the smallest allowed size in family C (min_capacity=0)." in (
        rec.reason
    )


def test_respects_min_capacity_floor(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("C2"), catalog, RedisRecommendRules(min_capacity=2))
    assert rec.target_sku is None
    assert "already the smallest allowed size in family C (min_capacity=2)." in rec.reason
    higher = recommend_redis(finding("C3"), catalog, RedisRecommendRules(min_capacity=2))
    assert higher.target_sku == "Standard C2"


def test_unknown_size_is_low_confidence(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("C9"), catalog, RedisRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "Size C9 not in config/redis-skus.yaml; cannot recommend." in rec.reason
    empty = recommend_redis(finding(""), catalog, RedisRecommendRules())
    assert empty.target_sku is None and empty.confidence == "low"
    assert "Size unknown not in config/redis-skus.yaml; cannot recommend." in empty.reason


def test_premium_reason_always_ends_with_the_tier_note(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("P2", tier="Premium"), catalog, RedisRecommendRules())
    assert rec.target_sku == "Premium P1"
    assert rec.reason.endswith(PREMIUM_NOTE)
    floor = recommend_redis(finding("P1", tier="premium"), catalog, RedisRecommendRules())
    assert floor.target_sku is None and floor.reason.endswith(PREMIUM_NOTE)
    unknown = recommend_redis(finding("P9", tier="Premium"), catalog, RedisRecommendRules())
    assert unknown.confidence == "low" and unknown.reason.endswith(PREMIUM_NOTE)
    no_fit = recommend_redis(
        finding("P2", tier="Premium", memory=90.0), catalog, RedisRecommendRules()
    )
    assert no_fit.target_sku is None and no_fit.reason.endswith(PREMIUM_NOTE)


def test_clustered_cache_mentions_the_shards(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("P2", tier="Premium", shards=3), catalog, RedisRecommendRules())
    assert rec.target_sku == "Premium P1"
    assert "Cache has 3 shards; the size applies per shard." in rec.reason
    assert rec.reason.endswith(PREMIUM_NOTE)  # the tier note stays last
    single = recommend_redis(
        finding("P2", tier="Premium", shards=1), catalog, RedisRecommendRules()
    )
    assert "shard" not in single.reason


def test_missing_memory_is_low_confidence_but_still_recommends(catalog: RedisSkuCatalog) -> None:
    rec = recommend_redis(finding("C2", memory=None), catalog, RedisRecommendRules())
    assert rec.target_sku == "Standard C1"
    assert rec.confidence == "low"
    assert "Memory not evaluated (no usedmemorypercentage data)." in rec.reason
    assert "fits the peak memory" not in rec.reason
    assert "Pricing" not in rec.reason


def test_catalog_get_is_case_insensitive_and_families_are_sorted_by_memory(
    catalog: RedisSkuCatalog,
) -> None:
    assert catalog.get("c1") == RedisSku(family="C", memory_gb=1)
    assert catalog.get("C1") is catalog.get("c1")
    assert catalog.get("X1") is None
    assert [n for n, _ in catalog.family_members("C")] == ["C0", "C1", "C2", "C3", "C4", "C5", "C6"]
    assert [n for n, _ in catalog.family_members("P")] == ["P1", "P2", "P3", "P4", "P5"]
    assert catalog.family_members("Z") == []
    shuffled = RedisSkuCatalog.model_validate(
        {"C2": {"family": "C", "memory_gb": 2.5}, "C0": {"family": "C", "memory_gb": 0.25}}
    )
    assert [n for n, _ in shuffled.family_members("C")] == ["C0", "C2"]


def test_capacity_index_reads_the_digits() -> None:
    assert capacity_index("C0") == 0 and capacity_index("P5") == 5 and capacity_index("C12") == 12
    assert capacity_index("") == -1 and capacity_index("C") == -1


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        RedisRecommendRules.model_validate({"min_capacity": 0, "bogus": 2})
    with pytest.raises(ValueError):
        RedisSku.model_validate({"family": "C", "memory_gb": 1, "vcpu": 2})


def test_default_rules() -> None:
    rules = RedisRecommendRules()
    assert rules.min_capacity == 0
    assert rules.headroom == 1.3
