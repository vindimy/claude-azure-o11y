from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from config.loader import deep_merge, load_config
from config.settings import Settings
from recommend.vm import VmRecommendRules


def test_load_default_config(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-nonprod")
    vm = cfg.thresholds.resource_types["vm"]
    assert vm.metrics["cpu"].ops_hot == 90 and vm.metrics["cpu"].finops_cold == 20
    assert isinstance(cfg.rules["vm"], VmRecommendRules) and cfg.rules["vm"].min_vcpu == 1
    assert cfg.sql_skus.vcore["managed_instance"] == [4, 8, 16, 24, 32, 40, 64, 80]
    assert cfg.postgres_skus.root == {}
    assert cfg.thresholds.tags.exclude == "o11y-exclude"
    assert "cloud-engineering" in cfg.assignment_groups.root


def test_mg_overlay_merges_partial_file(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    rules = cfg.rules["vm"]
    assert isinstance(rules, VmRecommendRules) and rules.min_vcpu == 2
    assert cfg.thresholds.resource_types["vm"].metrics["cpu"].ops_hot == 90


def test_deep_merge_nested() -> None:
    assert deep_merge({"a": {"b": 1, "c": 2}, "d": 1}, {"a": {"b": 9}}) == {
        "a": {"b": 9, "c": 2},
        "d": 1,
    }


def test_unknown_key_fails(tmp_path: Path, config_dir: Path) -> None:
    _copy_config(config_dir, tmp_path)
    (tmp_path / "thresholds" / "default.yaml").write_text(
        (config_dir / "thresholds" / "default.yaml").read_text() + "\nbogus: 1\n"
    )
    with pytest.raises(ValidationError):
        load_config(tmp_path, "mg-x")


def test_unknown_resource_type_fails(tmp_path: Path, config_dir: Path) -> None:
    _copy_config(config_dir, tmp_path)
    (tmp_path / "thresholds" / "default.yaml").write_text(
        (config_dir / "thresholds" / "default.yaml").read_text()
        + "  nope:\n    namespace: X/y\n    metrics: {}\n"
    )
    with pytest.raises(ValueError, match="unknown resource types.*known:"):
        load_config(tmp_path, "mg-x")


def test_unknown_recommend_knob_fails(tmp_path: Path, config_dir: Path) -> None:
    _copy_config(config_dir, tmp_path)
    (tmp_path / "thresholds" / "mg-x.yaml").write_text(
        "resource_types:\n  vm:\n    recommend:\n      bogus: 1\n"
    )
    with pytest.raises(ValidationError):
        load_config(tmp_path, "mg-x")


def test_settings_resource_type_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MG_ID", "mg-x")
    monkeypatch.delenv("RESOURCE_TYPES", raising=False)
    assert Settings().resource_type_list(["vm", "sqldb"]) == ["vm", "sqldb"]
    monkeypatch.setenv("RESOURCE_TYPES", "sqldb, vm")
    assert Settings().resource_type_list(["vm", "sqldb", "cosmos"]) == ["vm", "sqldb"]
    monkeypatch.setenv("RESOURCE_TYPES", "nope")
    with pytest.raises(ValueError, match="nope"):
        Settings().resource_type_list(["vm"])


def test_bad_regex_fails_startup(tmp_path: Path, config_dir: Path) -> None:
    _copy_config(config_dir, tmp_path)
    (tmp_path / "ignore.yaml").write_text("resource_groups:\n  - '^rg-(unclosed'\n")
    with pytest.raises(ValidationError):
        load_config(tmp_path, "mg-x")


def test_malformed_assignment_group_email_fails(tmp_path: Path, config_dir: Path) -> None:
    _copy_config(config_dir, tmp_path)
    (tmp_path / "assignment-groups.yaml").write_text("team-a:\n  email: not-an-email\n")
    with pytest.raises(ValidationError):
        load_config(tmp_path, "mg-x")


def test_settings_scope_prefers_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MG_ID", "mg-x")
    monkeypatch.setenv("SUBSCRIPTION_IDS", "a, b")
    s = Settings()
    assert s.subscription_id_list == ["a", "b"]
    assert s.scope.kind == "subscriptions"
    assert s.scope.values == ["a", "b"]


def test_settings_scope_management_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MG_ID", "mg-x")
    monkeypatch.delenv("SUBSCRIPTION_IDS", raising=False)
    s = Settings()
    assert s.scope.kind == "management_group"
    assert s.scope.values == ["mg-x"]


def _copy_config(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)
