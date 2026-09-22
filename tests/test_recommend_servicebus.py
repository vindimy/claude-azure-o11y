from __future__ import annotations

import pytest

from models import ColdFinding, ColdObservation, Resource
from recommend.servicebus import ServiceBusRecommendRules, recommend_servicebus

_SUFFIX = " Premium→Standard not evaluated (VNet, message size, feature checks)."


def finding(
    capacity: int,
    cpu: float,
    memory: float | None = None,
    percentile: int = 95,
    partitions: int = 1,
    tier: str = "Premium",
) -> ColdFinding:
    ns = Resource(
        kind="servicebus",
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.ServiceBus/namespaces/sb",
        name="sb",
        type="microsoft.servicebus/namespaces",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{tier} {capacity} MU",
        tags={},
        props={"tier": tier, "capacity": capacity, "partitions": partitions},
    )
    observations = [ColdObservation("cpu", percentile, cpu, cpu, 20, True, 1.0)]
    if memory is not None:
        observations.append(ColdObservation("memory", percentile, memory, memory, 30, True, 1.0))
    return ColdFinding(
        resource=ns,
        metric="cpu",
        observed=cpu,
        percentile=percentile,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(observations),
    )


def test_recommends_fewer_units_when_headroom_leaves_room() -> None:
    # 4 MU × 15 % × 1.3 = 0.78 MU needed -> 1 MU
    rec = recommend_servicebus(finding(4, 15.0, 10.0), ServiceBusRecommendRules())
    assert rec.target_sku == "Premium 1 MU" and rec.confidence == "medium"
    assert rec.reason.startswith("P95 CPU 15% over 14d is below 20%. P95 memory 10% is below 30%.")
    assert "Premium 1 MU covers the peak with 1.3x headroom." in rec.reason
    assert rec.reason.endswith(_SUFFIX)


def test_peak_is_the_larger_of_cpu_and_memory() -> None:
    # memory 28 % drives it: 4 × 0.28 × 1.3 = 1.46 -> 2 MU
    rec = recommend_servicebus(finding(4, 5.0, 28.0), ServiceBusRecommendRules())
    assert rec.target_sku == "Premium 2 MU"


def test_already_at_smallest_unit_gives_no_target() -> None:
    rec = recommend_servicebus(finding(1, 15.0, 10.0), ServiceBusRecommendRules())
    assert rec.target_sku is None and rec.confidence == "medium"
    assert "already at the smallest size that covers the peak (min_units=1)" in rec.reason
    assert rec.reason.endswith(_SUFFIX)


def test_min_units_is_a_floor() -> None:
    rec = recommend_servicebus(finding(4, 5.0, 5.0), ServiceBusRecommendRules(min_units=2))
    assert rec.target_sku == "Premium 2 MU"
    rec = recommend_servicebus(finding(2, 5.0, 5.0), ServiceBusRecommendRules(min_units=2))
    assert rec.target_sku is None


def test_missing_memory_lowers_confidence() -> None:
    rec = recommend_servicebus(finding(4, 15.0), ServiceBusRecommendRules())
    assert rec.target_sku == "Premium 1 MU" and rec.confidence == "low"
    assert "Memory not evaluated (no NamespaceMemoryUsage data)." in rec.reason


def test_unknown_capacity_cannot_recommend() -> None:
    rec = recommend_servicebus(finding(0, 15.0, 10.0), ServiceBusRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "Capacity unknown; cannot recommend." in rec.reason


def test_partitions_are_named_in_the_reason() -> None:
    rec = recommend_servicebus(finding(4, 15.0, 10.0, partitions=2), ServiceBusRecommendRules())
    assert "Namespace has 2 messaging partitions." in rec.reason
    rec = recommend_servicebus(finding(4, 15.0, 10.0, partitions=1), ServiceBusRecommendRules())
    assert "messaging partitions" not in rec.reason


def test_reason_interpolates_the_configured_percentile() -> None:
    rec = recommend_servicebus(finding(4, 15.0, 10.0, percentile=90), ServiceBusRecommendRules())
    assert "P90 CPU 15%" in rec.reason and "P90 memory 10%" in rec.reason
    assert "P95" not in rec.reason


def test_lower_cased_tier_keeps_its_casing() -> None:
    rec = recommend_servicebus(finding(4, 15.0, 10.0, tier="premium"), ServiceBusRecommendRules())
    assert rec.target_sku == "premium 1 MU"


def test_ladder_is_a_knob() -> None:
    rules = ServiceBusRecommendRules.model_validate({"messaging_units": [1, 2, 4, 8, 16, 32]})
    assert rules.messaging_units[-1] == 32 and rules.headroom == 1.3 and rules.min_units == 1
    # 16 MU at 60 %: 16 × 0.6 × 1.3 = 12.5 -> nothing below 16 covers it
    assert recommend_servicebus(finding(16, 60.0, 10.0), rules).target_sku is None
    # 32 MU at 30 %: 32 × 0.3 × 1.3 = 12.5 -> 16 MU
    assert recommend_servicebus(finding(32, 30.0, 10.0), rules).target_sku == "Premium 16 MU"


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        ServiceBusRecommendRules.model_validate({"headroom": 1.3, "bogus": 2})
