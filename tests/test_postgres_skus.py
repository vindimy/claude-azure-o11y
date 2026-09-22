from __future__ import annotations

from pathlib import Path

from config.loader import load_config


def test_catalog_lookup_is_case_insensitive(config_dir: Path) -> None:
    cat = load_config(config_dir, "mg-x").postgres_skus
    assert cat.get("standard_d4ds_v5") is not None
    assert cat.get("Standard_Nope") is None


def test_family_members_sorted_by_vcpu(config_dir: Path) -> None:
    cat = load_config(config_dir, "mg-x").postgres_skus
    members = cat.family_members("Ddsv5")
    vcpus = [s.vcpu for _, s in members]
    assert vcpus == sorted(vcpus)
    assert members[0][0] == "Standard_D2ds_v5"


def test_every_family_has_unique_vcpu_ladder(config_dir: Path) -> None:
    cat = load_config(config_dir, "mg-x").postgres_skus
    families = {s.family for s in cat.root.values()}
    for fam in families:
        vcpus = [s.vcpu for _, s in cat.family_members(fam)]
        # Burstable legitimately has two 2-vCPU sizes (B2s, B2ms); the rest are strict ladders
        if fam != "Burstable":
            assert len(vcpus) == len(set(vcpus)), fam
