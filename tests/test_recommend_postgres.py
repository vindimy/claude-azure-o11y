from __future__ import annotations

from pathlib import Path

import pytest

from config.loader import load_config
from config.models import VmSkuCatalog
from models import ColdFinding, ColdObservation, Resource
from recommend.postgres import PostgresRecommendRules, recommend_postgres


def finding(size: str, cpu_p95: float = 7.2, memory_p95: float | None = None) -> ColdFinding:
    server = Resource(
        kind="postgres",
        id=(
            "/subscriptions/s1/resourceGroups/rg/providers/"
            f"Microsoft.DBforPostgreSQL/flexibleServers/{size}"
        ),
        name=size,
        type="microsoft.dbforpostgresql/flexibleservers",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=size,
        tags={},
        props={"tier": "GeneralPurpose", "state": "Ready", "storage_gb": 128, "version": "14"},
    )
    observations = [ColdObservation("cpu", 95, cpu_p95, 3.0, 20, True, 1.0)]
    if memory_p95 is not None:
        observations.append(ColdObservation("memory", 95, memory_p95, 50.0, 30, True, 1.0))
    return ColdFinding(
        resource=server,
        metric="cpu",
        observed=cpu_p95,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(observations),
    )


@pytest.fixture
def catalog(config_dir: Path) -> VmSkuCatalog:
    return load_config(config_dir, "mg-x").postgres_skus


def test_next_size_down_same_family(catalog: VmSkuCatalog) -> None:
    rec = recommend_postgres(
        finding("Standard_D8ds_v5", memory_p95=15.0), catalog, PostgresRecommendRules()
    )
    assert rec.target_sku == "Standard_D4ds_v5"
    assert rec.confidence == "medium"
    assert "P95 CPU 7.2% over 14d is below 20%." in rec.reason
    assert "P95 memory 15% is below 30%." in rec.reason
    assert rec.reason.endswith("Pricing not implemented for PostgreSQL.")


def test_without_memory_is_low_confidence(catalog: VmSkuCatalog) -> None:
    rec = recommend_postgres(finding("Standard_D8ds_v5"), catalog, PostgresRecommendRules())
    assert rec.target_sku == "Standard_D4ds_v5"
    assert rec.confidence == "low"
    assert "Memory not evaluated (no memory_percent data)." in rec.reason


def test_respects_min_vcpu_floor(catalog: VmSkuCatalog) -> None:
    rec = recommend_postgres(
        finding("Standard_D8ds_v5", memory_p95=15.0),
        catalog,
        PostgresRecommendRules(min_vcpu=8),
    )
    assert rec.target_sku is None
    assert "min_vcpu=8" in rec.reason


def test_smallest_in_family_has_no_target(catalog: VmSkuCatalog) -> None:
    rec = recommend_postgres(finding("Standard_B1ms"), catalog, PostgresRecommendRules())
    assert rec.target_sku is None
    assert "already smallest allowed size" in rec.reason
    assert "Burstable" in rec.reason


def test_case_insensitive_sku_lookup(catalog: VmSkuCatalog) -> None:
    rec = recommend_postgres(finding("standard_d8ds_v5"), catalog, PostgresRecommendRules())
    assert rec.target_sku == "Standard_D4ds_v5"


def test_unknown_sku_low_confidence_no_target(catalog: VmSkuCatalog) -> None:
    rec = recommend_postgres(finding("Standard_NC6"), catalog, PostgresRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "not in config/postgres-skus.yaml" in rec.reason
    assert rec.reason.endswith("Pricing not implemented for PostgreSQL.")


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        PostgresRecommendRules.model_validate({"min_vcpu": 1, "bogus": 2})
