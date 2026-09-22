from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from config.loader import load_config
from config.models import VmSkuCatalog
from models import ColdFinding, ColdObservation, Resource
from recommend.ladder import fit_up, next_smaller
from recommend.vm import VmRecommendRules, recommend_vm, with_pricing


def finding(size: str, p95: float = 7.2, memory_p5: float | None = None) -> ColdFinding:
    vm = Resource(
        kind="vm",
        id=f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{size}",
        name=size,
        type="microsoft.compute/virtualmachines",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=size,
        tags={},
        props={"os_type": "Linux", "power_state": "PowerState/running"},
    )
    observations = [ColdObservation("cpu", 95, p95, 3.0, 20, True, 1.0)]
    if memory_p5 is not None:
        observations.append(ColdObservation("memory", 5, memory_p5, 90.0, 70, True, 1.0))
    return ColdFinding(
        resource=vm,
        metric="cpu",
        observed=p95,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(observations),
    )


@pytest.fixture
def catalog(config_dir: Path) -> VmSkuCatalog:
    return load_config(config_dir, "mg-x").vm_skus


def test_next_size_down_same_family(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_D8s_v5"), catalog, VmRecommendRules(min_vcpu=1))
    assert rec.target_sku == "Standard_D4s_v5"
    assert rec.confidence == "low"  # memory not observed
    assert "P95 CPU 7.2% over 14d is below 20%" in rec.reason
    assert "Memory not evaluated" in rec.reason
    assert "Dsv5" in rec.reason


def test_memory_clause_raises_confidence(catalog: VmSkuCatalog) -> None:
    rec = recommend_vm(finding("Standard_D8s_v5", memory_p5=75.0), catalog, VmRecommendRules())
    assert rec.target_sku == "Standard_D4s_v5" and rec.confidence == "medium"
    assert "P5 available memory 75% is above 70% (peak use 25%)" in rec.reason


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


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        VmRecommendRules.model_validate({"min_vcpu": 1, "bogus": 2})


def test_ladder_helpers() -> None:
    ladder = [2, 4, 8, 16]
    assert next_smaller(ladder, 8, 1) == 4
    assert next_smaller(ladder, 8, 8) is None
    assert next_smaller(ladder, 2, 1) is None
    assert fit_up(ladder, 2.6, 2, 8) == 4
    assert fit_up(ladder, 1.0, 4, 8) == 4
    assert fit_up(ladder, 9.0, 2, 8) is None
    assert fit_up(ladder, 1.0, 1, 2) is None
