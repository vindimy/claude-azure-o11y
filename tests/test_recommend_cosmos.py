from __future__ import annotations

from models import ColdFinding, ColdObservation, Resource
from recommend.cosmos import CosmosRecommendRules, recommend_cosmos

VERIFY = "Account-level value; verify per container."


def finding(
    p95: float,
    median: float = 5.0,
    *,
    provisioned: float | None = None,
    autoscale_max: float | None = None,
) -> ColdFinding:
    account = Resource(
        kind="cosmos",
        id=(
            "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.DocumentDB"
            "/databaseAccounts/cosmos-cold"
        ),
        name="cosmos-cold",
        type="microsoft.documentdb/databaseaccounts",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku="provisioned",
        tags={},
        props={
            "api_kind": "GlobalDocumentDB",
            "capacity_mode": "provisioned",
            "enable_free_tier": False,
        },
    )
    inputs = {}
    if provisioned is not None:
        inputs["provisioned"] = provisioned
    if autoscale_max is not None:
        inputs["autoscale_max"] = autoscale_max
    return ColdFinding(
        resource=account,
        metric="ru",
        observed=p95,
        percentile=95,
        threshold=30,
        lookback_days=14,
        coverage=1.0,
        observations=(ColdObservation("ru", 95, p95, median, 30, True, 1.0),),
        inputs=inputs,
    )


def test_provisioned_account_gets_a_lower_manual_throughput() -> None:
    rec = recommend_cosmos(finding(10.0, 8.0, provisioned=4000.0), CosmosRecommendRules())
    assert rec.target_sku == "600 RU/s" and rec.confidence == "low"
    assert "P95 normalized RU 10% over 14d is below 30% (median 8%)." in rec.reason
    assert "Lower provisioned throughput from 4000 RU/s." in rec.reason
    assert rec.reason.endswith(VERIFY)


def test_autoscale_account_gets_a_lower_autoscale_max() -> None:
    rec = recommend_cosmos(
        finding(10.0, 8.0, provisioned=4000.0, autoscale_max=10000.0), CosmosRecommendRules()
    )
    assert rec.target_sku == "autoscale 1300 RU/s max"
    assert "Lower the autoscale max from 10000 RU/s." in rec.reason


def test_bursty_provisioned_account_is_pointed_at_autoscale() -> None:
    rec = recommend_cosmos(finding(12.0, 3.0, provisioned=4000.0), CosmosRecommendRules())
    assert rec.target_sku == "autoscale 700 RU/s max"
    assert "P95/median ratio 4.0 suggests autoscale." in rec.reason


def test_without_a_throughput_input_no_target_is_sized() -> None:
    rec = recommend_cosmos(finding(10.0, 8.0), CosmosRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "No provisioned throughput metric; cannot size." in rec.reason


def test_target_at_or_above_current_throughput_is_not_recommended() -> None:
    rec = recommend_cosmos(finding(25.0, 20.0, provisioned=400.0), CosmosRecommendRules())
    assert rec.target_sku is None
    assert "Sized target 400 RU/s is not below current 400 RU/s." in rec.reason


def test_rules_are_configurable() -> None:
    rules = CosmosRecommendRules(min_ru=1000, headroom=2.0, autoscale_ratio=10.0)
    rec = recommend_cosmos(finding(12.0, 3.0, provisioned=4000.0), rules)
    # headroom 2.0 -> 4000 * 12% * 2 = 960 -> 1000 RU/s floor; ratio 4.0 is below 10.0, so manual.
    assert rec.target_sku == "1000 RU/s"
