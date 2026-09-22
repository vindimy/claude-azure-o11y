from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from config.loader import deep_merge, load_config
from config.models import FamilySkuCatalog, MetricThreshold, SqlSkuCatalog
from config.settings import Settings
from recommend.vm import VmRecommendRules
from resource_types import TYPES
from tests.conftest import load_fixture


def test_load_default_config(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-nonprod")
    vm = cfg.thresholds.resource_types["vm"]
    assert vm.metrics["cpu"].ops_hot == 90 and vm.metrics["cpu"].finops_cold == 20
    assert isinstance(cfg.rules["vm"], VmRecommendRules) and cfg.rules["vm"].min_vcpu == 1
    sql = cfg.catalog_for("sqlmi", SqlSkuCatalog)
    assert sql.vcore["managed_instance"] == [4, 8, 16, 24, 32, 40, 64, 80]
    assert sql.dtu["Standard"]["S3"] == 100 and sql.pool_edtu["Basic"][0] == 50
    assert cfg.catalog_for("postgres", FamilySkuCatalog).get("Standard_D4ds_v5") is not None
    assert cfg.thresholds.tags.exclude == "o11y-exclude"
    assert "cloud-engineering" in cfg.assignment_groups.root


def test_mg_overlay_merges_partial_file(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    rules = cfg.rules["vm"]
    assert isinstance(rules, VmRecommendRules) and rules.min_vcpu == 2
    assert cfg.thresholds.resource_types["vm"].metrics["cpu"].ops_hot == 90


def test_catalogs_come_from_each_type_spec(config_dir: Path) -> None:
    """Adding a type declares its catalog in SPEC; the loader and AppConfig do not change."""
    cfg = load_config(config_dir, "mg-x")
    assert set(cfg.catalogs) == {k for k, spec in TYPES.items() if spec.catalog is not None}
    # sqldb, sqlpool, and sqlmi share sql-skus.yaml: one parse, one object
    assert cfg.catalogs["sqldb"] is cfg.catalogs["sqlpool"] is cfg.catalogs["sqlmi"]
    with pytest.raises(TypeError, match="catalog for vm"):
        cfg.catalog_for("vm", SqlSkuCatalog)
    with pytest.raises(TypeError, match="rules for vm"):
        cfg.rules_for("vm", SqlSkuCatalog)
    with pytest.raises(KeyError):
        cfg.catalog_for("vnet", FamilySkuCatalog)


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


def test_one_raw_metric_with_two_aggregations_fails_startup(
    tmp_path: Path, config_dir: Path
) -> None:
    """requests_for() de-duplicates by raw name, so the second key would use the first's series."""
    _copy_config(config_dir, tmp_path)
    (tmp_path / "thresholds" / "mg-x.yaml").write_text(
        "resource_types:\n"
        "  eventhub:\n"
        "    metrics:\n"
        "      ingress_peak:\n"
        "        metric_name: IncomingBytes\n"
        "        aggregation: Maximum\n"
        "        finops_cold: 30\n"
    )
    with pytest.raises(ValidationError, match="two aggregations"):
        load_config(tmp_path, "mg-x")


def _parsed_props(kind: str, rules: BaseModel) -> set[str]:
    """Props as the pipeline sees them: parsed, then enriched with the type's rules."""
    name = "resource_graph_vms_page1.json" if kind == "vm" else f"{kind}/resource_graph.json"
    spec = TYPES[kind]
    props: set[str] = set()
    for row in load_fixture(name)["data"]:
        props |= set(spec.enrich(spec.parse(row), rules).props)
    return props


def test_every_configured_prop_exists_in_the_parser_output(config_dir: Path) -> None:
    """Spec §10: a misspelled prop resolves to "" and silently stops a metric being evaluated."""
    cfg = load_config(config_dir, "mg-prod")
    for kind, type_cfg in cfg.thresholds.resource_types.items():
        props = _parsed_props(kind, cfg.rules[kind])
        assert props, kind
        for key, metric in type_cfg.metrics.items():
            for prop in metric.applies_to:
                assert prop in props, f"{kind}.{key}: applies_to prop {prop!r}"
            if metric.capacity_prop:
                assert metric.capacity_prop in props, f"{kind}.{key}: capacity_prop"
            if TYPES[kind].metric_source == "inventory" and metric.inputs:
                assert metric.inputs[0] in props, f"{kind}.{key}: inventory input"


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


def test_one_raw_metric_with_two_filters_fails_startup(tmp_path: Path, config_dir: Path) -> None:
    _copy_config(config_dir, tmp_path)
    (tmp_path / "thresholds" / "mg-x.yaml").write_text(
        "resource_types:\n"
        "  eventhub:\n"
        "    metrics:\n"
        "      throttled_by_entity:\n"
        "        metric_name: ThrottledRequests\n"
        "        aggregation: Total\n"
        "        dimension: {name: EntityName, values: [hub-a]}\n"
        "        ops_hot: 1\n"
    )
    with pytest.raises(ValidationError, match="two dimension filters"):
        load_config(tmp_path, "mg-x")


def test_dimension_filter_renders_odata_and_needs_a_value() -> None:
    m = MetricThreshold.model_validate(
        {"metric_name": "Transactions", "dimension": {"name": "ResponseType", "values": ["A", "B"]}}
    )
    assert m.filter == "ResponseType eq 'A' or ResponseType eq 'B'"
    assert MetricThreshold(metric_name="x").filter is None
    with pytest.raises(ValidationError):
        MetricThreshold.model_validate(
            {"metric_name": "Transactions", "dimension": {"name": "ResponseType", "values": []}}
        )
