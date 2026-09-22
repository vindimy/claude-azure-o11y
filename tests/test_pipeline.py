from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import resource_types
from config.loader import load_config
from config.settings import Settings
from metrics.batch import MetricWindow
from models import MetricPoint, MetricRequest, Resource, Scope, Series
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import Clients, FindingsWriteFailed, run
from resource_types.registry import ResourceTypeSpec
from resource_types.vm import parse as parse_vm
from tests.conftest import load_fixture

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeInventory:
    """Serves the recorded fixtures per kind; unknown kinds are empty."""

    def __init__(self, failing: set[str] | None = None) -> None:
        self.failing = failing or set()
        self.kinds: list[str] = []

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        self.kinds.append(kind)
        if kind in self.failing:
            raise RuntimeError(f"inventory for {kind} is down")
        if kind == "vm":
            rows = (
                load_fixture("resource_graph_vms_page1.json")["data"]
                + load_fixture("resource_graph_vms_page2.json")["data"]
            )
            return [parse_vm(r) for r in rows]
        return []


# Series returned per resource name; every metric name not listed returns no points.
FAKE_VALUES: dict[str, dict[str, float]] = {
    "vm-hot": {"Percentage CPU": 96.0, "Available Memory Percentage": 5.0},
    "vm-cold": {"Percentage CPU": 4.0, "Available Memory Percentage": 80.0},
}


class FakeMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, timedelta]] = []
        self.requests: list[list[MetricRequest]] = []
        self.granularities: list[timedelta] = []

    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metrics: list[MetricRequest],
        window: MetricWindow,
    ) -> dict[str, Series]:
        self.calls.append((region, subscription_id, len(resource_ids), window.end - window.start))
        self.requests.append(metrics)
        self.granularities.append(window.granularity)
        n = int((window.end - window.start) / window.granularity)
        out: dict[str, Series] = {}
        for rid in resource_ids:
            name = rid.rsplit("/", 1)[1]
            values = FAKE_VALUES.get(name, {})
            out[rid] = {}
            for m in metrics:
                v = values.get(m.name)
                vals = [v] * n if v is not None else []
                out[rid][m.name] = [
                    MetricPoint(window.start + i * window.granularity, x)
                    for i, x in enumerate(vals)
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
        metrics: list[MetricRequest],
        window: MetricWindow,
    ) -> dict[str, Series]:
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


def make(
    tmp_path: Path, config_dir: Path, resource_types: str = ""
) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types=resource_types,
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


COMMON_SKIPS = {"ignored_rg": 1, "excluded_by_tag": 1, "not_running": 1}


def vm_rows(tmp_path: Path, table: str) -> list[dict[str, Any]]:
    return [r for r in rows(tmp_path, table) if r["ResourceType"].endswith("virtualmachines")]


async def test_ops_run_writes_hot_rows_only(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics = make(tmp_path, config_dir, resource_types="vm")
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.mode == "ops"
    assert summary.inventory_total == 6 and summary.evaluated == 3
    assert summary.ignored_rg_count == 1
    assert summary.skips["no_ops_data"] >= 1  # vm-nodata has no CPU
    assert {k: v for k, v in summary.skips.items() if k in COMMON_SKIPS} == COMMON_SKIPS
    assert [v.name for v in summary.excluded] == ["vm-excluded"]
    # one region/subscription -> 1 chunk, Ops window only, every ops metric in one call
    assert metrics.calls == [("eastus", "s1", 3, timedelta(minutes=60))]
    assert MetricRequest("Percentage CPU", "Average") in metrics.requests[0]
    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []
    assert summary.by_type["vm"].inventory_total == 6 and summary.by_type["vm"].kept == 3

    cpu = [r for r in rows(tmp_path, OPS_TABLE) if r["MetricKey"] == "cpu"]
    assert [r["ResourceName"] for r in cpu] == ["vm-hot"]
    [hot] = cpu
    assert hot["RunId"] == "run-1"
    assert hot["MetricName"] == "Percentage CPU" and hot["ObservedValue"] == 96.0
    assert hot["Threshold"] == 90 and hot["ThresholdSource"] == "config"
    assert hot["Aggregation"] == "Average"
    assert hot["ResourceType"] == "microsoft.compute/virtualmachines"
    assert (
        hot["WindowStart"] == "2026-09-15T11:00:00Z" and hot["WindowEnd"] == "2026-09-15T12:00:00Z"
    )
    assert hot["AssignmentGroupEmail"] == "cloud-engineering@example.com"
    assert hot["MissingTags"] == []


async def test_finops_run_writes_cold_rows_only(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics = make(tmp_path, config_dir, resource_types="vm")
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.findings == 1 and summary.rows_written == 1
    assert summary.skips == {**COMMON_SKIPS, "insufficient_finops_data": 1}
    assert metrics.calls == [("eastus", "s1", 3, timedelta(days=14))]
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "vm-cold" and cold["Sku"] == "Standard_D8s_v5"
    assert cold["MetricKey"] == "cpu" and cold["Percentile"] == 95
    assert cold["RecommendedSku"] == "Standard_D4s_v5" and cold["ObservedValue"] == 4.0
    assert cold["OsType"] == "Windows"
    assert cold["CurrentMonthlyCost"] == 560.64 and cold["ProjectedMonthlyCost"] == 280.32
    assert cold["EstimatedMonthlySaving"] == 280.32 and cold["Currency"] == "USD"
    assert cold["WindowStart"] == "2026-09-01T12:00:00Z"
    assert cold["MissingTags"] == ["owner", "assignment_group"]


async def test_every_ops_run_writes_a_row_while_hot(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir, resource_types="vm")
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, "ops", now=NOW)
    await run(settings, cfg, clients, "ops", now=NOW + timedelta(minutes=15))
    written = [r for r in rows(tmp_path, OPS_TABLE) if r["MetricKey"] == "cpu"]
    assert [r["ResourceName"] for r in written] == ["vm-hot", "vm-hot"]
    assert written[0]["RunId"] != written[1]["RunId"]


async def test_failed_write_fails_the_run(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir, resource_types="vm")
    clients.findings = FailingSink({FINOPS_TABLE})
    with pytest.raises(FindingsWriteFailed):
        await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)


async def test_metrics_failure_is_counted_not_fatal(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir, resource_types="vm")
    clients.metrics = Boom()
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "ops", now=NOW)
    assert summary.chunk_failures == 1 and summary.by_type["vm"].chunk_failures == 1
    assert summary.skips.get("chunk_failed") == 3
    assert summary.rows_written == 0 and summary.destinations == {}


async def test_resource_types_setting_narrows_and_validates(
    tmp_path: Path, config_dir: Path
) -> None:
    settings, clients, _ = make(tmp_path, config_dir, resource_types="vm")
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, "ops", now=NOW)
    inventory = clients.inventory
    assert isinstance(inventory, FakeInventory) and inventory.kinds == ["vm"]

    settings, clients, _ = make(tmp_path, config_dir, resource_types="nope")
    with pytest.raises(ValueError, match="RESOURCE_TYPES"):
        await run(settings, cfg, clients, "ops", now=NOW)


async def test_default_runs_every_configured_type(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _ = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, "ops", now=NOW)
    inventory = clients.inventory
    assert isinstance(inventory, FakeInventory)
    assert inventory.kinds == list(cfg.thresholds.resource_types)


async def test_failing_type_does_not_stop_the_run(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A throwaway type registered only here fails its inventory; VM rows are still written."""
    broken = ResourceTypeSpec(kind="broken", arm_type="x/y", query="resources", parse=parse_vm)
    monkeypatch.setitem(resource_types.TYPES, "broken", broken)
    cfg = load_config(config_dir, "mg-prod")
    cfg.thresholds.resource_types["broken"] = cfg.thresholds.resource_types["vm"]
    settings, clients, _ = make(tmp_path, config_dir, resource_types="broken,vm")
    clients.inventory = FakeInventory(failing={"broken"})
    summary = await run(settings, cfg, clients, "ops", now=NOW)
    assert summary.type_failures == 1
    assert [r["ResourceName"] for r in rows(tmp_path, OPS_TABLE) if r["MetricKey"] == "cpu"] == [
        "vm-hot"
    ]
