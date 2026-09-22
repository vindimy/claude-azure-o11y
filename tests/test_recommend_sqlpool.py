from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import load_config
from config.models import SqlSkuCatalog
from models import ColdFinding, Resource
from recommend.sqlpool import SqlPoolRecommendRules, recommend_sqlpool


def finding(
    tier: str, sku_name: str, capacity: int, purchasing_model: str, observed: float = 15.0
) -> ColdFinding:
    metric = "dtu" if purchasing_model == "dtu" else "cpu"
    pool = Resource(
        kind="sqlpool",
        id=(
            "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Sql/servers/srv1"
            f"/elasticPools/{sku_name.lower()}"
        ),
        name=sku_name.lower(),
        type="microsoft.sql/servers/elasticpools",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{sku_name} {capacity}",
        tags={},
        props={
            "tier": tier,
            "sku_name": sku_name,
            "capacity": capacity,
            "purchasing_model": purchasing_model,
            "state": "Ready",
        },
    )
    return ColdFinding(
        resource=pool,
        metric=metric,
        observed=observed,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
    )


@pytest.fixture
def catalog(config_dir: Path) -> SqlSkuCatalog:
    return load_config(config_dir, "mg-x").catalog_for("sqlpool", SqlSkuCatalog)


def test_dtu_ladder_recommends_next_smaller_pool(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlpool(
        finding("Standard", "StandardPool", 100, "dtu", observed=15.0),
        catalog,
        SqlPoolRecommendRules(),
    )
    assert rec.target_sku == "StandardPool 50"
    assert rec.confidence == "medium"
    assert "P95 DTU 15% over 14d is below 20%" in rec.reason
    assert "50 eDTU covers P95 with 1.3x headroom." in rec.reason
    assert "Pricing" not in rec.reason


def test_vcore_ladder_recommends_next_smaller_pool(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlpool(
        finding("GeneralPurpose", "GP_Gen5", 8, "vcore", observed=10.0),
        catalog,
        SqlPoolRecommendRules(),
    )
    assert rec.target_sku == "GP_Gen5 2"
    assert rec.confidence == "medium"
    assert "P95 CPU 10% over 14d is below 20%" in rec.reason
    assert "2 vCores cover P95 with 1.3x headroom." in rec.reason
    assert "Pricing" not in rec.reason


def test_dtu_pool_already_smallest(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlpool(
        finding("Standard", "StandardPool", 50, "dtu", observed=15.0),
        catalog,
        SqlPoolRecommendRules(),
    )
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "is already the smallest Standard pool size." in rec.reason


def test_vcore_pool_already_at_min_vcores(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlpool(
        finding("GeneralPurpose", "GP_Gen5", 2, "vcore", observed=10.0),
        catalog,
        SqlPoolRecommendRules(min_vcores=2),
    )
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "already at min_vcores=2." in rec.reason


def test_unknown_tier_low_confidence_no_target(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqlpool(
        finding("Hyperscale", "HS_Gen5", 40, "dtu", observed=15.0),
        catalog,
        SqlPoolRecommendRules(),
    )
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "Hyperscale not in config/sql-skus.yaml; cannot recommend." in rec.reason


def test_vcore_unknown_ladder_or_capacity_low_confidence() -> None:
    rec = recommend_sqlpool(
        finding("GeneralPurpose", "GP_Gen5", 8, "vcore", observed=10.0),
        SqlSkuCatalog(),  # empty catalog: no vcore ladder at all
        SqlPoolRecommendRules(),
    )
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "vCore ladder or capacity unknown; cannot recommend." in rec.reason


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        SqlPoolRecommendRules.model_validate({"min_vcores": 2, "bogus": 1})
