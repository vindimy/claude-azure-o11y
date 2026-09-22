from __future__ import annotations

import pytest

from config.models import FamilySkuCatalog
from models import ColdFinding, ColdObservation, Resource
from recommend.appserviceplan import AppServicePlanRecommendRules, recommend_appserviceplan

CATALOG = FamilySkuCatalog.model_validate(
    {
        "S1": {"family": "S", "vcpu": 1, "memory_gib": 1.75},
        "S2": {"family": "S", "vcpu": 2, "memory_gib": 3.5},
        "S3": {"family": "S", "vcpu": 4, "memory_gib": 7},
        "P0v3": {"family": "Pv3", "vcpu": 1, "memory_gib": 4},
        "P1v3": {"family": "Pv3", "vcpu": 2, "memory_gib": 8},
        "P2v3": {"family": "Pv3", "vcpu": 4, "memory_gib": 16},
        "EP1": {"family": "EP", "vcpu": 1, "memory_gib": 3.5},
        "EP2": {"family": "EP", "vcpu": 2, "memory_gib": 7},
    }
)
RULES = AppServicePlanRecommendRules()


def finding(
    sku_name: str,
    instances: int,
    cpu: float,
    memory: float | None = 10.0,
    *,
    sites: int = 2,
    elastic: bool = False,
) -> ColdFinding:
    plan = Resource(
        kind="appserviceplan",
        id=f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Web/serverfarms/asp-{sku_name}",
        name=f"asp-{sku_name}",
        type="microsoft.web/serverfarms",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{sku_name} x{instances}",
        tags={},
        props={
            "sku_name": sku_name,
            "tier": "PremiumV3",
            "instances": instances,
            "status": "Ready",
            "sites": sites,
            "elastic_scale": elastic,
        },
    )
    observations = [
        ColdObservation(
            metric="cpu",
            percentile=95,
            value=cpu,
            median=cpu / 2,
            threshold=20,
            cold=True,
            coverage=1.0,
        )
    ]
    if memory is not None:
        observations.append(
            ColdObservation(
                metric="memory",
                percentile=95,
                value=memory,
                median=memory / 2,
                threshold=40,
                cold=True,
                coverage=1.0,
            )
        )
    return ColdFinding(
        resource=plan,
        metric="cpu",
        observed=cpu,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(observations),
    )


def test_recommends_fewer_instances_when_headroom_leaves_room() -> None:
    # peak 30% of 3 instances: ceil(3 * 0.3 * 1.3) = ceil(1.17) = 2 instances.
    rec = recommend_appserviceplan(finding("P1v3", 3, 10.0, 30.0), CATALOG, RULES)
    assert rec.target_sku == "P1v3 x2"
    assert rec.confidence == "medium"
    assert "P95 CPU 10% over 14d is below 20%." in rec.reason
    assert "P95 memory 30% is below 40%." in rec.reason
    assert rec.reason.endswith("2 instances cover the peak with 1.3x headroom.")
    assert "pric" not in rec.reason.lower()


def test_peak_is_the_busier_of_cpu_and_memory() -> None:
    # Memory 30% > CPU 10%, so memory sizes the target: 4 * 0.3 * 1.3 = 1.56 -> 2, not 1.
    rec = recommend_appserviceplan(finding("P1v3", 4, 10.0, 30.0), CATALOG, RULES)
    assert rec.target_sku == "P1v3 x2"


def test_at_instance_floor_steps_down_the_family() -> None:
    # One instance, 10% peak: P0v3 (1 vCPU) takes 10 * 1.3 * 2 / 1 = 26% -> fits.
    rec = recommend_appserviceplan(finding("P1v3", 1, 10.0, 10.0), CATALOG, RULES)
    assert rec.target_sku == "P0v3 x1"
    assert rec.confidence == "medium"
    assert "Instance count is at floor; P0v3 x1 covers the peak with 1.3x headroom." in rec.reason


def test_smaller_sku_that_would_not_fit_gives_no_target() -> None:
    # 45% peak on 2 vCPU: on 1 vCPU that is 45 * 1.3 * 2 = 117% -> does not fit.
    rec = recommend_appserviceplan(finding("P1v3", 1, 15.0, 45.0), CATALOG, RULES)
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "P0v3 would not fit the peak with 1.3x headroom (117% of 1 vCPU)." in rec.reason


def test_smallest_allowed_size_in_family_gives_no_target() -> None:
    rec = recommend_appserviceplan(finding("S1", 1, 10.0, 10.0), CATALOG, RULES)
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "S1 is already smallest allowed size in family S (min_vcpu=1)." in rec.reason


def test_min_vcpu_blocks_the_family_step() -> None:
    rules = AppServicePlanRecommendRules(min_vcpu=2)
    rec = recommend_appserviceplan(finding("P1v3", 1, 10.0, 10.0), CATALOG, rules)
    assert rec.target_sku is None
    assert "already smallest allowed size in family Pv3 (min_vcpu=2)" in rec.reason


def test_unknown_sku_gives_no_target_with_low_confidence() -> None:
    rec = recommend_appserviceplan(finding("X9", 1, 10.0, 10.0), CATALOG, RULES)
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert rec.reason.endswith("SKU X9 not in config/appservice-skus.yaml; cannot recommend.")


def test_empty_plan_is_a_delete() -> None:
    rec = recommend_appserviceplan(finding("S1", 1, 1.0, 5.0, sites=0), CATALOG, RULES)
    assert rec.target_sku == "delete"
    assert rec.confidence == "medium"
    assert rec.reason.endswith("The plan hosts no apps (0 sites); delete it.")


def test_empty_plan_wins_over_the_instance_lever() -> None:
    rec = recommend_appserviceplan(finding("P1v3", 3, 1.0, 5.0, sites=0), CATALOG, RULES)
    assert rec.target_sku == "delete"


def test_elastic_plan_skips_the_instance_lever() -> None:
    # 3 instances at 10% would scale in, but elastic scale owns the count: EP2 -> EP1 instead.
    rec = recommend_appserviceplan(finding("EP2", 3, 10.0, 10.0, elastic=True), CATALOG, RULES)
    assert rec.target_sku == "EP1 x3"
    assert "Elastic scale manages the instance count; EP1 x3 covers the peak" in rec.reason


def test_elastic_plan_at_smallest_sku_gives_no_target() -> None:
    rec = recommend_appserviceplan(finding("EP1", 3, 10.0, 10.0, elastic=True), CATALOG, RULES)
    assert rec.target_sku is None
    assert "Elastic scale manages the instance count; EP1 is already smallest" in rec.reason


def test_instance_lever_that_cannot_scale_in_falls_through_to_the_family() -> None:
    # 2 instances at 70% peak: ceil(2 * 0.7 * 1.3) = 2 -> no scale-in; P0v3 would be 182%.
    rec = recommend_appserviceplan(finding("P1v3", 2, 15.0, 70.0), CATALOG, RULES)
    assert rec.target_sku is None
    assert "P0v3 would not fit" in rec.reason


def test_min_instances_is_a_floor_for_the_instance_lever() -> None:
    rules = AppServicePlanRecommendRules(min_instances=2)
    rec = recommend_appserviceplan(finding("P1v3", 3, 5.0, 5.0), CATALOG, rules)
    assert rec.target_sku == "P1v3 x2"  # ceil(3 * 0.05 * 1.3) = 1, floored to 2
    rec = recommend_appserviceplan(finding("P1v3", 2, 5.0, 5.0), CATALOG, rules)
    assert rec.target_sku == "P0v3 x2"  # at floor: family step at the same count


def test_missing_memory_is_low_confidence_and_says_so() -> None:
    rec = recommend_appserviceplan(finding("P1v3", 3, 10.0, None), CATALOG, RULES)
    assert rec.target_sku == "P1v3 x1"  # ceil(3 * 0.1 * 1.3) = 1
    assert rec.confidence == "low"
    assert "Memory not evaluated (no MemoryPercentage data)." in rec.reason


def test_reason_interpolates_the_configured_percentile() -> None:
    f = finding("P1v3", 3, 10.0, 30.0)
    f = ColdFinding(
        resource=f.resource,
        metric="cpu",
        observed=10.0,
        percentile=90,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(
            ColdObservation(
                metric=o.metric,
                percentile=90,
                value=o.value,
                median=o.median,
                threshold=o.threshold,
                cold=o.cold,
                coverage=o.coverage,
            )
            for o in f.observations
        ),
    )
    rec = recommend_appserviceplan(f, CATALOG, RULES)
    assert "P90 CPU 10%" in rec.reason and "P90 memory 30%" in rec.reason
    assert "P95" not in rec.reason


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        AppServicePlanRecommendRules.model_validate({"headroom": 1.3, "bogus": 2})


def test_default_rules() -> None:
    rules = AppServicePlanRecommendRules()
    assert rules.min_instances == 1 and rules.min_vcpu == 1 and rules.headroom == 1.3
