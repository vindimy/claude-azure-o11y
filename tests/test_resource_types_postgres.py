"""End-to-end pipeline test for the postgres resource type.

Its own FakeInventory serves the postgres fixture only, per context-core.md: tests/test_pipeline.py
is shared by other resource-type worktrees and must not be edited. FakeMetrics/FakePricing/rows/
FAKE_VALUES are reused from there by import.
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
from resource_types.postgres import parse as parse_postgres
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeInventory:
    """Serves the postgres fixture only; every other kind is empty."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == "postgres":
            return [parse_postgres(r) for r in load_fixture("postgres/resource_graph.json")["data"]]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types="postgres",
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


async def test_ops_run_flags_hot_server_and_skips_stopped(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "pg-ready", {"cpu_percent": 96.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.inventory_total == 2
    assert summary.by_type["postgres"].kept == 1
    assert summary.skips.get("not_ready") == 1

    cpu = [r for r in rows(tmp_path, OPS_TABLE) if r["MetricKey"] == "cpu"]
    assert [r["ResourceName"] for r in cpu] == ["pg-ready"]
    [hot] = cpu
    assert hot["ObservedValue"] == 96.0 and hot["Threshold"] == 90
    assert hot["ResourceType"] == "microsoft.dbforpostgresql/flexibleservers"


async def test_finops_run_recommends_smaller_sku(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "pg-ready", {"cpu_percent": 4.0, "memory_percent": 15.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, "finops", now=NOW)

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "pg-ready" and cold["Sku"] == "Standard_D4ds_v5"
    assert cold["MetricKey"] == "cpu" and cold["Percentile"] == 95
    assert cold["RecommendedSku"] == "Standard_D2ds_v5"
    assert cold["Confidence"] == "medium"
    assert cold["Reason"].endswith("Pricing not implemented for PostgreSQL.")
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None


async def test_not_ready_server_is_skipped_from_finops_too(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "pg-ready", {"cpu_percent": 4.0, "memory_percent": 15.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)
    assert summary.skips.get("not_ready") == 1
    assert [r["ResourceName"] for r in rows(tmp_path, FINOPS_TABLE)] == ["pg-ready"]
