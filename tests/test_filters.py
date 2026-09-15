from __future__ import annotations

from pathlib import Path

from config.loader import load_config
from inventory.filters import filter_vms
from inventory.vms import parse_vm_row
from tests.conftest import load_fixture


def _all_rows() -> list[dict[str, object]]:
    page1: list[dict[str, object]] = load_fixture("resource_graph_vms_page1.json")["data"]
    page2: list[dict[str, object]] = load_fixture("resource_graph_vms_page2.json")["data"]
    return page1 + page2


def test_filters_partition_and_count(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    vms = [parse_vm_row(r) for r in _all_rows()]
    result = filter_vms(vms, cfg.thresholds.tags, cfg.ignore.patterns_for("mg-prod"))
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
    result = filter_vms([parse_vm_row(row)], cfg.thresholds.tags, [])
    assert len(result.kept) == 1 and not result.excluded
