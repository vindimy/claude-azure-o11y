from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.loader import load_config
from config.settings import Settings
from inventory.vms import parse_vm_row
from metrics.batch import MetricWindow
from models import MetricPoint, Scope, VmResource
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import Clients, FindingsWriteFailed, run
from tests.conftest import load_fixture

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeInventory:
    async def list_vms(self, scope: Scope) -> list[VmResource]:
        rows = (
            load_fixture("resource_graph_vms_page1.json")["data"]
            + load_fixture("resource_graph_vms_page2.json")["data"]
        )
        return [parse_vm_row(r) for r in rows]


class FakeMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, timedelta]] = []

    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metric_name: str,
        window: MetricWindow,
    ) -> dict[str, list[MetricPoint]]:
        self.calls.append((region, subscription_id, len(resource_ids), window.end - window.start))
        n = int((window.end - window.start) / window.granularity)
        out: dict[str, list[MetricPoint]] = {}
        for rid in resource_ids:
            name = rid.rsplit("/", 1)[1]
            if name == "vm-hot":
                vals = [96.0] * n
            elif name == "vm-cold":
                vals = [4.0] * n
            else:
                vals = []
            out[rid] = [
                MetricPoint(window.start + i * window.granularity, v) for i, v in enumerate(vals)
            ]
        return out


class FakePricing:
    async def monthly_price(self, region: str, sku: str, os_type: str) -> Decimal | None:
        prices = {"standard_d8s_v5": Decimal("560.64"), "standard_d4s_v5": Decimal("280.32")}
        return prices.get(sku.lower())


class Boom:
    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metric_name: str,
        window: MetricWindow,
    ) -> dict[str, list[MetricPoint]]:
        raise RuntimeError("boom")


class FailingSink:
    def __init__(self, fail: set[str]) -> None:
        self.fail = fail
        self.written: dict[str, list[dict[str, Any]]] = {}

    async def write(self, table: str, rows: list[dict[str, Any]]) -> str:
        if table in self.fail:
            raise RuntimeError("ingestion down")
        self.written.setdefault(table, []).extend(rows)
        return f"mem:{table}"


def rows(tmp_path: Path, table: str) -> list[dict[str, Any]]:
    path = tmp_path / "findings" / f"{table}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(mg_id="mg-prod", dry_run=True, output_dir=tmp_path, config_dir=config_dir)
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


COMMON_SKIPS = {"ignored_rg": 1, "excluded_by_tag": 1, "not_running": 1}


async def test_ops_run_writes_hot_rows_only(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.mode == "ops"
    assert summary.inventory_total == 6 and summary.evaluated == 3
    assert summary.findings == 1 and summary.rows_written == 1
    assert summary.ignored_rg_count == 1
    assert summary.skips == {**COMMON_SKIPS, "no_ops_data": 1}
    assert [v.name for v in summary.excluded] == ["vm-excluded"]
    # one region/subscription -> 1 chunk, Ops window only
    assert metrics.calls == [("eastus", "s1", 3, timedelta(minutes=60))]
    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []

    [hot] = rows(tmp_path, OPS_TABLE)
    assert hot["ResourceName"] == "vm-hot" and hot["RunId"] == "run-1"
    assert hot["MetricName"] == "Percentage CPU" and hot["ObservedValue"] == 96.0
    assert hot["Threshold"] == 90 and hot["ThresholdSource"] == "config"
    assert hot["Aggregation"] == "average"
    assert (
        hot["WindowStart"] == "2026-09-15T11:00:00Z" and hot["WindowEnd"] == "2026-09-15T12:00:00Z"
    )
    assert hot["AssignmentGroupEmail"] == "cloud-engineering@example.com"
    assert hot["MissingTags"] == []


async def test_finops_run_writes_cold_rows_only(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.findings == 1 and summary.rows_written == 1
    assert summary.skips == {**COMMON_SKIPS, "insufficient_finops_data": 1}
    assert metrics.calls == [("eastus", "s1", 3, timedelta(days=14))]
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "vm-cold" and cold["Sku"] == "Standard_D8s_v5"
    assert cold["RecommendedSku"] == "Standard_D4s_v5" and cold["ObservedValue"] == 4.0
    assert cold["CurrentMonthlyCost"] == 560.64 and cold["ProjectedMonthlyCost"] == 280.32
    assert cold["EstimatedMonthlySaving"] == 280.32 and cold["Currency"] == "USD"
    assert cold["WindowStart"] == "2026-09-01T12:00:00Z"
    assert cold["MissingTags"] == ["owner", "assignment_group"]


async def test_every_ops_run_writes_a_row_while_hot(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, "ops", now=NOW)
    await run(settings, cfg, clients, "ops", now=NOW + timedelta(minutes=15))
    written = rows(tmp_path, OPS_TABLE)
    assert [r["ResourceName"] for r in written] == ["vm-hot", "vm-hot"]
    assert written[0]["RunId"] != written[1]["RunId"]


async def test_failed_write_fails_the_run(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir)
    clients.findings = FailingSink({FINOPS_TABLE})
    with pytest.raises(FindingsWriteFailed):
        await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)


async def test_metrics_failure_is_counted_not_fatal(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir)
    clients.metrics = Boom()
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "ops", now=NOW)
    assert summary.chunk_failures == 1
    assert summary.skips.get("chunk_failed") == 3
    assert summary.rows_written == 0 and summary.destinations == {}
