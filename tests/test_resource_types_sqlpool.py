"""Parser, readiness filter, and end-to-end pipeline dispatch for Azure SQL elastic pools."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from config.loader import load_config
from config.settings import Settings
from inventory.filters import filter_resources
from metrics.batch import parse_batch_response
from models import MetricRequest, Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import UNPRICED_NOTE, Clients, run
from resource_types.sqlpool import active, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def _rows() -> list[dict[str, object]]:
    data: list[dict[str, object]] = load_fixture("sqlpool/resource_graph.json")["data"]
    return data


def test_parser_maps_tier_sku_capacity_and_purchasing_model() -> None:
    dtu_pool, vcore_pool, _ = (parse(r) for r in _rows())

    assert dtu_pool.name == "pool-dtu"
    assert dtu_pool.sku == "StandardPool 100"
    assert dtu_pool.prop("tier") == "Standard"
    assert dtu_pool.prop("sku_name") == "StandardPool"
    assert dtu_pool.prop("capacity") == 100
    assert dtu_pool.prop("purchasing_model") == "dtu"
    assert dtu_pool.prop("state") == "Ready"

    assert vcore_pool.sku == "GP_Gen5 8"
    assert vcore_pool.prop("purchasing_model") == "vcore"
    assert vcore_pool.prop("capacity") == 8


def test_active_keeps_ready_pools_and_skips_others() -> None:
    dtu_pool, vcore_pool, disabled_pool = (parse(r) for r in _rows())
    assert active(dtu_pool) is None
    assert active(vcore_pool) is None
    skip = active(disabled_pool)
    assert skip is not None
    assert skip.reason == "not_ready"
    assert skip.resource_id == disabled_pool.id


def test_filters_partition_by_readiness(config_dir: Path) -> None:
    cfg = load_config(config_dir, "mg-prod")
    pools = [parse(r) for r in _rows()]
    result = filter_resources(pools, cfg.thresholds.tags, [], active)
    assert [p.name for p in result.kept] == ["pool-dtu", "pool-vcore"]
    assert {(s.resource_id.rsplit("/", 1)[1], s.reason) for s in result.skips} == {
        ("pool-disabled", "not_ready")
    }


class FakeInventory:
    """Serves the recorded sqlpool fixture; every other kind is empty (single-type test)."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == "sqlpool":
            return [parse(r) for r in _rows()]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types="sqlpool",
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


async def test_ops_run_dispatches_dtu_and_cpu_by_purchasing_model(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES, "pool-dtu", {"dtu_consumption_percent": 96.0, "storage_percent": 50.0}
    )
    monkeypatch.setitem(FAKE_VALUES, "pool-vcore", {"cpu_percent": 95.0, "storage_percent": 40.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.by_type["sqlpool"].inventory_total == 3
    assert summary.by_type["sqlpool"].kept == 2
    assert summary.skips["not_ready"] == 1

    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [
        ("pool-dtu", "dtu"),
        ("pool-vcore", "cpu"),
    ]
    dtu_row = next(r for r in ops if r["MetricKey"] == "dtu")
    assert dtu_row["MetricName"] == "dtu_consumption_percent"
    assert dtu_row["ObservedValue"] == 96.0 and dtu_row["Threshold"] == 90
    assert dtu_row["ResourceType"] == "microsoft.sql/servers/elasticpools"
    assert dtu_row["Sku"] == "StandardPool 100"


async def test_finops_run_recommends_smaller_pool(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "pool-dtu", {"dtu_consumption_percent": 15.0})
    monkeypatch.setitem(FAKE_VALUES, "pool-vcore", {"cpu_percent": 10.0})
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.findings == 2
    fin = {r["ResourceName"]: r for r in rows(tmp_path, FINOPS_TABLE)}
    assert set(fin) == {"pool-dtu", "pool-vcore"}

    dtu = fin["pool-dtu"]
    assert dtu["MetricKey"] == "dtu" and dtu["Percentile"] == 95
    assert dtu["RecommendedSku"] == "StandardPool 50"
    assert dtu["Confidence"] == "medium"
    assert dtu["Reason"].endswith(UNPRICED_NOTE)
    assert dtu["CurrentMonthlyCost"] is None and dtu["ProjectedMonthlyCost"] is None

    vcore = fin["pool-vcore"]
    assert vcore["MetricKey"] == "cpu"
    assert vcore["RecommendedSku"] == "GP_Gen5 2"
    assert vcore["Confidence"] == "medium"


def test_batch_response_maps_pool_metrics() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    prefix = (
        "/subscriptions/s1/resourceGroups/rg-sql-prod/providers/Microsoft.Sql/servers/sql-srv1/"
        "elasticPools/"
    )
    dtu_pool, vcore_pool = prefix + "pool-dtu", prefix + "pool-vcore"
    dtu = MetricRequest("dtu_consumption_percent", "Average")
    cpu = MetricRequest("cpu_percent", "Average")
    storage = MetricRequest("storage_percent", "Average")
    out = parse_batch_response(
        load_fixture("sqlpool/metrics_batch.json"), [dtu_pool, vcore_pool], [dtu, cpu, storage]
    )
    # resource ids are matched case-insensitively (the payload upper-cases pool-dtu)
    assert [p.value for p in out[dtu_pool][dtu.name]] == [92.0, 95.5, None]
    assert [p.value for p in out[dtu_pool][storage.name]] == [40.0]
    assert out[dtu_pool][cpu.name] == []
    assert [p.value for p in out[vcore_pool][cpu.name]] == [8.0, 12.5]
    assert out[vcore_pool][dtu.name] == [] and out[vcore_pool][storage.name] == []
