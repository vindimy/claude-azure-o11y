from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import load_config
from config.models import SqlSkuCatalog
from models import ColdFinding, ColdObservation, Resource
from recommend.sqldb import SqlDbRecommendRules, recommend_sqldb

NOTE = " Pricing not implemented for SQL."


def finding(
    sku_name: str,
    tier: str,
    model: str,
    capacity: int,
    metric: str = "dtu",
    observed: float = 20.0,
    threshold: float = 25.0,
) -> ColdFinding:
    db = Resource(
        kind="sqldb",
        id=f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Sql/servers/sql/databases/{sku_name}",
        name="db-1",
        type="microsoft.sql/servers/databases",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=sku_name,
        tags={},
        props={
            "tier": tier,
            "sku_name": sku_name,
            "capacity": capacity,
            "purchasing_model": model,
            "hyperscale": False,
            "pool_id": "",
            "status": "Online",
            "db_kind": "v12.0,user",
        },
    )
    return ColdFinding(
        resource=db,
        metric=metric,
        observed=observed,
        percentile=95,
        threshold=threshold,
        lookback_days=14,
        coverage=1.0,
        observations=(ColdObservation(metric, 95, observed, 5.0, threshold, True, 1.0),),
    )


@pytest.fixture
def catalog(config_dir: Path) -> SqlSkuCatalog:
    return load_config(config_dir, "mg-x").sql_skus


def test_dtu_database_drops_to_the_objective_that_covers_headroom(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqldb(finding("S3", "Standard", "dtu", 100), catalog, SqlDbRecommendRules())
    assert rec.target_sku == "S2" and rec.confidence == "medium"
    assert "P95 DTU 20% over 14d is below 25%." in rec.reason
    assert "50 DTU covers P95 with 1.3x headroom." in rec.reason
    assert rec.reason.endswith(NOTE)


def test_vcore_database_drops_to_the_smallest_covering_size(catalog: SqlSkuCatalog) -> None:
    cold = finding("GP_Gen5_8", "GeneralPurpose", "vcore", 8, metric="cpu", observed=10.0)
    rec = recommend_sqldb(cold, catalog, SqlDbRecommendRules())
    assert rec.target_sku == "GP_Gen5_2" and rec.confidence == "medium"
    assert "P95 CPU 10% over 14d is below 25%." in rec.reason
    assert "2 vCores cover P95 with 1.3x headroom." in rec.reason
    assert rec.reason.endswith(NOTE)


def test_min_vcores_is_the_floor(catalog: SqlSkuCatalog) -> None:
    cold = finding("GP_Gen5_8", "GeneralPurpose", "vcore", 8, metric="cpu", observed=10.0)
    rec = recommend_sqldb(cold, catalog, SqlDbRecommendRules(min_vcores=4))
    assert rec.target_sku == "GP_Gen5_4" and rec.confidence == "medium"


def test_unknown_tier_cannot_be_recommended(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqldb(finding("PRS1", "PremiumRS", "dtu", 125), catalog, SqlDbRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "PRS1 not in config/sql-skus.yaml; cannot recommend." in rec.reason
    assert rec.reason.endswith(NOTE)


def test_smallest_dtu_objective_has_nowhere_to_go(catalog: SqlSkuCatalog) -> None:
    rec = recommend_sqldb(finding("Basic", "Basic", "dtu", 5), catalog, SqlDbRecommendRules())
    assert rec.target_sku is None and rec.confidence == "medium"
    assert "Basic is already the smallest Basic objective." in rec.reason
    assert rec.reason.endswith(NOTE)


def test_database_already_at_min_vcores(catalog: SqlSkuCatalog) -> None:
    cold = finding("GP_Gen5_2", "GeneralPurpose", "vcore", 2, metric="cpu", observed=10.0)
    rec = recommend_sqldb(cold, catalog, SqlDbRecommendRules())
    assert rec.target_sku is None and rec.confidence == "medium"
    assert "GP_Gen5_2 is already at min_vcores=2." in rec.reason
    assert rec.reason.endswith(NOTE)


def test_unknown_capacity_cannot_be_recommended(catalog: SqlSkuCatalog) -> None:
    cold = finding("GP_Gen5", "GeneralPurpose", "vcore", 0, metric="cpu", observed=10.0)
    rec = recommend_sqldb(cold, catalog, SqlDbRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "vCore ladder or capacity unknown; cannot recommend." in rec.reason
    assert rec.reason.endswith(NOTE)


def test_empty_catalog_cannot_be_recommended() -> None:
    cold = finding("GP_Gen5_8", "GeneralPurpose", "vcore", 8, metric="cpu", observed=10.0)
    rec = recommend_sqldb(cold, SqlSkuCatalog(), SqlDbRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "vCore ladder or capacity unknown; cannot recommend." in rec.reason
