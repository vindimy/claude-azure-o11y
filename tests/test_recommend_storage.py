from __future__ import annotations

import pytest
from pydantic import ValidationError

from models import ColdFinding, ColdObservation, Resource
from recommend.storage import StorageRecommendRules, recommend_storage

GIB = 2**30
VERIFY = "Account-level; verify per container and check early-deletion and retrieval charges."


def finding(
    p95: float = 100.0,
    median: float = 40.0,
    *,
    used_gib: float | None = None,
    percentile: int = 95,
    lookback_days: int = 14,
    sku_name: str = "Standard_LRS",
) -> ColdFinding:
    account = Resource(
        kind="storage",
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/stcold",
        name="stcold",
        type="microsoft.storage/storageaccounts",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{sku_name} Hot",
        tags={},
        props={
            "sku_name": sku_name,
            "tier": "Standard",
            "account_kind": "StorageV2",
            "access_tier": "Hot",
            "state": "Succeeded",
            "hns": False,
            "tierable": True,
        },
    )
    inputs = {} if used_gib is None else {"used_capacity": used_gib * GIB}
    return ColdFinding(
        resource=account,
        metric="transactions",
        observed=p95,
        percentile=percentile,
        threshold=1000,
        lookback_days=lookback_days,
        coverage=1.0,
        observations=(ColdObservation("transactions", percentile, p95, median, 1000, True, 1.0),),
        inputs=inputs,
    )


def test_hot_account_with_enough_data_is_pointed_at_the_cool_tier() -> None:
    rec = recommend_storage(finding(used_gib=500), StorageRecommendRules())
    assert rec.target_sku == "Standard_LRS Cool" and rec.confidence == "low"
    assert rec.reason.startswith("P95 hourly transactions 100 over 14d is below 1000.")
    assert "500 GiB in the Hot tier: set the default access tier to Cool" in rec.reason
    assert "or add a lifecycle rule." in rec.reason
    assert rec.reason.endswith(VERIFY)
    assert "price" not in rec.reason.lower()


def test_target_tier_is_configurable() -> None:
    rec = recommend_storage(
        finding(used_gib=500, sku_name="Standard_GRS"), StorageRecommendRules(target_tier="Cold")
    )
    assert rec.target_sku == "Standard_GRS Cold"
    assert "set the default access tier to Cold" in rec.reason


def test_small_accounts_get_no_target() -> None:
    rec = recommend_storage(finding(used_gib=50), StorageRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "50 GiB is below min_gib=100; tiering saves too little." in rec.reason
    # min_gib is a knob: a lower floor turns the same account into a recommendation.
    assert recommend_storage(finding(used_gib=50), StorageRecommendRules(min_gib=10)).target_sku


def test_without_used_capacity_no_target_is_sized() -> None:
    rec = recommend_storage(finding(), StorageRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert rec.reason == (
        "P95 hourly transactions 100 over 14d is below 1000. Used capacity unknown; cannot size."
    )


def test_evidence_reports_the_observed_percentile_and_window() -> None:
    rec = recommend_storage(
        finding(12.5, used_gib=200, percentile=90, lookback_days=30), StorageRecommendRules()
    )
    assert rec.reason.startswith("P90 hourly transactions 12.5 over 30d is below 1000.")


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        StorageRecommendRules(min_gb=100)  # type: ignore[call-arg]
