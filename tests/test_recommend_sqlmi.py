from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import load_config
from config.models import SqlSkuCatalog
from models import ColdFinding, Resource
from recommend.sqlmi import SqlMiRecommendRules, recommend_sqlmi


def finding(vcores: int, sku_name: str = "GP_Gen5", observed: float = 4.0) -> ColdFinding:
    mi = Resource(
        kind="sqlmi",
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Sql/managedInstances/mi",
        name="mi",
        type="microsoft.sql/managedinstances",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{sku_name} {vcores} vCores",
        tags={},
        props={
            "tier": "GeneralPurpose",
            "sku_name": sku_name,
            "vcores": vcores,
            "storage_gb": 512,
            "state": "Ready",
        },
    )
    return ColdFinding(
        resource=mi,
        metric="cpu",
        observed=observed,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
    )


@pytest.fixture
def catalog(config_dir: Path) -> SqlSkuCatalog:
    return load_config(config_dir, "mg-x").catalog_for("sqlmi", SqlSkuCatalog)


def test_recommends_next_smaller_vcore_tier(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlmi(finding(vcores=8), catalog, SqlMiRecommendRules())
    assert rec.target_sku == "GP_Gen5 4 vCores"
    assert rec.confidence == "medium"
    assert "P95 CPU 4% over 14d is below 20%." in rec.reason
    assert "4 vCores cover P95 with 1.3x headroom." in rec.reason
    assert "Pricing" not in rec.reason


def test_already_at_min_vcores(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlmi(finding(vcores=4), catalog, SqlMiRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "GP_Gen5 4 vCores is already at min_vcores=4." in rec.reason
    assert "Pricing" not in rec.reason


def test_respects_min_vcores_floor(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlmi(finding(vcores=16), catalog, SqlMiRecommendRules(min_vcores=8))
    assert rec.target_sku == "GP_Gen5 8 vCores"
    assert rec.confidence == "medium"


def test_zero_vcores_is_low_confidence(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlmi(finding(vcores=0), catalog, SqlMiRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "vCore ladder or capacity unknown; cannot recommend." in rec.reason
    assert "Pricing" not in rec.reason


def test_empty_ladder_is_low_confidence() -> None:
    empty = SqlSkuCatalog(vcore={})
    rec = recommend_sqlmi(finding(vcores=8), empty, SqlMiRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "vCore ladder or capacity unknown; cannot recommend." in rec.reason


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        SqlMiRecommendRules.model_validate({"min_vcores": 4, "bogus": 2})


def test_default_rules() -> None:
    rules = SqlMiRecommendRules()
    assert rules.min_vcores == 4
    assert rules.headroom == 1.3
