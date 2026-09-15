from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from config.loader import load_config
from config.models import VmRecommendRules, VmSkuCatalog
from models import ColdFinding, VmResource
from recommend.vm import recommend_vm, with_pricing


def finding(size: str, p95: float = 7.2) -> ColdFinding:
    vm = VmResource(
        id=f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{size}",
        name=size,
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        vm_size=size,
        os_type="Linux",
        power_state="PowerState/running",
        tags={},
    )
    return ColdFinding(
        resource=vm, metric="cpu", observed_p95=p95, threshold=20, lookback_days=14, coverage=1.0
    )


@pytest.fixture
def catalog(config_dir: Path) -> VmSkuCatalog:
    return load_config(config_dir, "mg-x").vm_skus


def test_next_size_down_same_family(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_D8s_v5"), catalog, VmRecommendRules(min_vcpu=1))
    assert rec.target_sku == "Standard_D4s_v5"
    assert rec.confidence == "medium"
    assert "P95 CPU 7.2% over 14d is below 20%" in rec.reason
    assert "Memory not evaluated" in rec.reason
    assert "Dsv5" in rec.reason


def test_never_crosses_family(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_E2s_v5"), catalog, VmRecommendRules(min_vcpu=1))
    assert rec.target_sku is None
    assert "already smallest allowed size" in rec.reason


def test_respects_min_vcpu_floor(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_D4s_v5"), catalog, VmRecommendRules(min_vcpu=4))
    assert rec.target_sku is None
    assert "min_vcpu=4" in rec.reason


def test_floor_allows_exactly_min_vcpu(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_D4s_v5"), catalog, VmRecommendRules(min_vcpu=2))
    assert rec.target_sku == "Standard_D2s_v5"


def test_case_insensitive_sku_lookup(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("standard_d8s_v5"), catalog, VmRecommendRules())
    assert rec.target_sku == "Standard_D4s_v5"


def test_unknown_sku_low_confidence_no_target(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_NC6"), catalog, VmRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "low"
    assert "not in config/vm-skus.yaml" in rec.reason


def test_with_pricing_computes_saving(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_D8s_v5"), catalog, VmRecommendRules())
    priced = with_pricing(rec, Decimal("280.32"), Decimal("140.16"))
    assert priced.saving == Decimal("140.16")
    assert with_pricing(rec, Decimal("1"), None).saving is None
