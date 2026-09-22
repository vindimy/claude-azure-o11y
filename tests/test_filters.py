from __future__ import annotations

from pathlib import Path

from config.loader import load_config
from inventory.filters import filter_resources
from models import Resource, Skip
from resource_types.registry import keep
from resource_types.vm import active, parse
from tests.conftest import load_fixture


def _all_rows() -> list[dict[str, object]]:
    page1: list[dict[str, object]] = load_fixture("resource_graph_vms_page1.json")["data"]
    page2: list[dict[str, object]] = load_fixture("resource_graph_vms_page2.json")["data"]
    return page1 + page2


def test_filters_partition_and_count(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    vms = [parse(r) for r in _all_rows()]
    result = filter_resources(vms, cfg.thresholds.tags, cfg.ignore.patterns_for("mg-prod"), active)
    assert [v.name for v in result.kept] == ["vm-hot", "vm-cold", "vm-nodata"]
    assert result.ignored_rg_count == 1
    assert [v.name for v in result.excluded] == ["vm-excluded"]
    assert {(s.resource_id.rsplit("/", 1)[1], s.reason) for s in result.skips} == {
        ("vm-dev", "ignored_rg"),
        ("vm-stopped", "not_running"),
        ("vm-excluded", "excluded_by_tag"),
    }


def test_exclude_tag_value_must_be_true(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    row = dict(load_fixture("resource_graph_vms_page2.json")["data"][3])
    row["tags"] = {"o11y-exclude": "false"}
    result = filter_resources([parse(row)], cfg.thresholds.tags, [], active)
    assert len(result.kept) == 1 and not result.excluded


def test_type_active_check_is_pluggable(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    r = Resource("x", "/r/1", "r1", "t", "s", "rg", "eastus", "sku", {}, {"state": "Paused"})
    assert filter_resources([r], cfg.thresholds.tags, [], keep).kept == [r]

    def paused(res: Resource) -> Skip | None:
        return Skip(res.id, "not_online", str(res.prop("state")))

    result = filter_resources([r], cfg.thresholds.tags, [], paused)
    assert result.kept == [] and result.skips == [Skip("/r/1", "not_online", "Paused")]
