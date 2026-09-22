"""End-to-end pipeline coverage for the Azure SQL Managed Instance (sqlmi) resource type.

Owns its own FakeInventory (serves only sqlmi fixtures) per context-core.md; does not touch
tests/test_pipeline.py, which is shared with other resource types built in parallel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from config.loader import load_config
from config.settings import Settings
from models import Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import Clients, run
from resource_types.sqlmi import active
from resource_types.sqlmi import parse as parse_sqlmi
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeInventory:
    """Serves the recorded sqlmi fixture; every other kind is empty."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        self.kinds.append(kind)
        if kind == "sqlmi":
            return [parse_sqlmi(r) for r in load_fixture("sqlmi/resource_graph.json")["data"]]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types="sqlmi",
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


def test_active_is_case_insensitive_about_the_state() -> None:
    """A lower-cased state must not skip every managed instance as not_ready (issue 1)."""
    row = dict(load_fixture("sqlmi/resource_graph.json")["data"][0], state="ready")
    assert active(parse_sqlmi(row)) is None
    stopped = parse_sqlmi(dict(row, state="Stopped"))
    skip = active(stopped)
    assert skip is not None and skip.reason == "not_ready" and skip.detail == "Stopped"
    assert stopped.prop("state") == "Stopped"  # display casing is kept


async def test_stopped_instance_is_skipped_not_ready(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "sqlmi-1", {"avg_cpu_percent": 4.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW)

    assert summary.by_type["sqlmi"].inventory_total == 2
    assert summary.by_type["sqlmi"].kept == 1
    assert summary.skips.get("not_ready") == 1


async def test_ops_run_writes_cpu_and_derived_storage_rows(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "sqlmi-1",
        {
            "avg_cpu_percent": 96.0,
            "storage_space_used_mb": 950.0,
            "reserved_storage_mb": 1000.0,
        },
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW)

    ops = rows(tmp_path, OPS_TABLE)
    assert sorted(r["MetricKey"] for r in ops) == ["cpu", "storage"]

    cpu = next(r for r in ops if r["MetricKey"] == "cpu")
    assert cpu["ResourceName"] == "sqlmi-1" and cpu["ObservedValue"] == 96.0
    assert cpu["MetricName"] == "avg_cpu_percent" and cpu["Aggregation"] == "Average"
    assert cpu["Sku"] == "GP_Gen5 8 vCores"
    assert cpu["ResourceType"] == "microsoft.sql/managedinstances"

    storage = next(r for r in ops if r["MetricKey"] == "storage")
    assert storage["MetricName"] == "storage_percent"
    assert storage["Aggregation"] == "Average (derived)"
    assert storage["ObservedValue"] == pytest.approx(95.0)
    assert storage["Threshold"] == 90

    assert summary.by_type["sqlmi"].findings == 2
    assert [m.name for m in metrics.requests[0]] == [
        "avg_cpu_percent",
        "storage_space_used_mb",
        "reserved_storage_mb",
    ]


async def test_finops_run_recommends_smaller_vcore_tier(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "sqlmi-1", {"avg_cpu_percent": 4.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, "finops", now=NOW)

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "sqlmi-1" and cold["Sku"] == "GP_Gen5 8 vCores"
    assert cold["MetricKey"] == "cpu" and cold["Percentile"] == 95
    assert cold["ObservedValue"] == 4.0
    assert cold["RecommendedSku"] == "GP_Gen5 4 vCores"
    assert cold["Confidence"] == "medium"
    assert "Pricing not implemented for SQL." in cold["Reason"]
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None
