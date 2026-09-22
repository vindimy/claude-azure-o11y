from __future__ import annotations

from typing import Any

import pytest

from models import ColdFinding, Resource
from recommend.appgateway import AppGatewayRecommendRules, recommend_appgateway


def finding(
    observed: float,
    *,
    sku_name: str = "Standard_v2",
    autoscale: bool = False,
    capacity: int = 0,
    min_capacity: int = 0,
    max_capacity: int = 0,
    reserved: int | None = None,
    percentile: int = 95,
) -> ColdFinding:
    props: dict[str, Any] = {
        "sku_name": sku_name,
        "tier": sku_name,
        "v2": True,
        "autoscale": autoscale,
        "capacity": capacity,
        "min_capacity": min_capacity,
        "max_capacity": max_capacity,
        "state": "Running",
    }
    if reserved is not None:
        props["reserved_capacity_units"] = reserved
    sku = (
        f"{sku_name} autoscale {min_capacity}-{max_capacity}"
        if autoscale
        else f"{sku_name} x{capacity}"
    )
    agw = Resource(
        kind="appgateway",
        id=(
            "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Network/"
            "applicationGateways/agw"
        ),
        name="agw",
        type="microsoft.network/applicationgateways",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=sku,
        tags={},
        props=props,
    )
    return ColdFinding(
        resource=agw,
        metric="capacity",
        observed=observed,
        percentile=percentile,
        threshold=30,
        lookback_days=14,
        coverage=1.0,
    )


def test_lowers_the_autoscale_minimum_and_keeps_the_maximum() -> None:
    # 40 CU reserved × 20% × 1.3 = 10.4 CU -> 2 instances.
    rec = recommend_appgateway(
        finding(20.0, autoscale=True, min_capacity=4, max_capacity=10, reserved=40),
        AppGatewayRecommendRules(),
    )
    assert rec.target_sku == "Standard_v2 autoscale 2-10"
    assert rec.confidence == "medium"
    assert rec.reason == (
        "P95 capacity units 20% of 40 reserved (4 instances × 10 CU) over 14d is below 30%. "
        "Lower the minimum instance count from 4 to 2."
    )


def test_lowers_the_fixed_instance_count() -> None:
    # 30 CU reserved × 20% × 1.3 = 7.8 CU -> 1 instance.
    rec = recommend_appgateway(
        finding(20.0, sku_name="WAF_v2", capacity=3, reserved=30), AppGatewayRecommendRules()
    )
    assert rec.target_sku == "WAF_v2 x1"
    assert rec.confidence == "medium"
    assert "P95 capacity units 20% of 30 reserved (3 instances × 10 CU)" in rec.reason
    assert rec.reason.endswith("Lower the instance count from 3 to 1.")


def test_already_at_the_floor_gives_no_target() -> None:
    rec = recommend_appgateway(finding(20.0, capacity=1, reserved=10), AppGatewayRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert rec.reason.endswith(
        "1 instances is already at the smallest instance count that covers the load "
        "(min_instances=1)."
    )
    # A configured floor above the computed count is honoured too.
    floored = recommend_appgateway(
        finding(20.0, capacity=2, reserved=20), AppGatewayRecommendRules(min_instances=2)
    )
    assert floored.target_sku is None and "(min_instances=2)" in floored.reason


def test_unknown_capacity_gives_no_target_with_low_confidence() -> None:
    no_reserved = recommend_appgateway(finding(20.0, capacity=3), AppGatewayRecommendRules())
    assert no_reserved.target_sku is None and no_reserved.confidence == "low"
    assert "capacity unknown" in no_reserved.reason
    assert no_reserved.reason.startswith("P95 capacity units 20% of 0 reserved (3 instances")
    no_instances = recommend_appgateway(
        finding(20.0, autoscale=True, min_capacity=0, max_capacity=5, reserved=10),
        AppGatewayRecommendRules(),
    )
    assert no_instances.target_sku is None and no_instances.confidence == "low"


def test_target_rounds_up_to_whole_instances() -> None:
    # 50 CU reserved × 10% × 1.3 = 6.5 CU -> 0.65 instances -> ceil to 1, not 0.
    rec = recommend_appgateway(finding(10.0, capacity=5, reserved=50), AppGatewayRecommendRules())
    assert rec.target_sku == "Standard_v2 x1"
    # 50 CU × 25% × 1.3 = 16.25 CU -> 1.625 instances -> 2.
    rec = recommend_appgateway(finding(25.0, capacity=5, reserved=50), AppGatewayRecommendRules())
    assert rec.target_sku == "Standard_v2 x2"


def test_reason_interpolates_the_configured_percentile() -> None:
    rec = recommend_appgateway(
        finding(20.0, capacity=3, reserved=30, percentile=90), AppGatewayRecommendRules()
    )
    assert "P90 capacity units 20% of 30 reserved" in rec.reason
    assert "P95" not in rec.reason


def test_cu_per_instance_is_a_knob_used_in_the_evidence_and_the_target() -> None:
    # 75 CU reserved (3 × 25) × 20% × 1.3 = 19.5 CU -> 0.78 instances -> 1.
    rec = recommend_appgateway(
        finding(20.0, capacity=3, reserved=75), AppGatewayRecommendRules(cu_per_instance=25)
    )
    assert rec.target_sku == "Standard_v2 x1"
    assert "(3 instances × 25 CU)" in rec.reason


def test_reason_never_mentions_pricing() -> None:
    rec = recommend_appgateway(finding(20.0, capacity=3, reserved=30), AppGatewayRecommendRules())
    assert "price" not in rec.reason.lower() and "cost" not in rec.reason.lower()
    assert rec.current_monthly is None and rec.projected_monthly is None


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        AppGatewayRecommendRules.model_validate({"headroom": 1.3, "bogus": 2})


def test_default_knobs() -> None:
    rules = AppGatewayRecommendRules()
    assert rules.cu_per_instance == 10 and rules.min_instances == 1 and rules.headroom == 1.3
