from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from config.loader import load_config
from config.settings import Settings
from metrics.batch import parse_batch_response
from models import MetricRequest, Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import UNPRICED_NOTE, Clients, run
from resource_types.appserviceplan import KIND, QUERY, active, finops_skip, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def plan_rows() -> list[dict[str, object]]:
    data: list[dict[str, object]] = load_fixture("appserviceplan/resource_graph.json")["data"]
    return data


def plan(name: str) -> Resource:
    return parse(next(r for r in plan_rows() if r["name"] == name))


class FakeInventory:
    """Serves the appserviceplan fixture rows; every other kind returns nothing."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == KIND:
            return [parse(r) for r in plan_rows()]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types=KIND,
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


# --- Parser -----------------------------------------------------------------


def test_query_excludes_consumption_and_flex_plans() -> None:
    assert "'dynamic', 'flexconsumption'" in QUERY and "!in" in QUERY
    assert "order by id asc" in QUERY


def test_parse_premium_plan() -> None:
    p = plan("asp-hot")
    assert p.kind == "appserviceplan" and p.type == "microsoft.web/serverfarms"
    assert p.sku == "P1v3 x3"
    assert p.prop("sku_name") == "P1v3" and p.prop("tier") == "PremiumV3"
    assert p.prop("instances") == 3 and p.prop("sites") == 4
    assert p.prop("status") == "Ready" and p.prop("elastic_scale") is False
    assert p.prop("os") == "linux" and p.prop("plan_kind") == "linux"
    assert p.tags["assignment_group"] == "cloud-engineering"


def test_parse_windows_and_elastic_plans() -> None:
    cold = plan("asp-cold")
    assert cold.prop("os") == "windows" and cold.prop("plan_kind") == "app"
    elastic = plan("asp-elastic")
    assert elastic.sku == "EP1 x3" and elastic.prop("elastic_scale") is True
    assert elastic.prop("tier") == "ElasticPremium"


def test_parse_handles_missing_fields() -> None:
    row = {
        "id": "/x",
        "name": "n",
        "subscriptionId": "s",
        "resourceGroup": "RG",
        "location": "eastus",
        "tags": None,
        "kind": None,
        "skuName": None,
        "tier": None,
        "capacity": None,
        "status": None,
        "sites": None,
        "elastic": None,
        "reserved": None,
    }
    p = parse(row)
    assert p.tags == {} and p.sku == ""
    assert p.prop("sku_name") == "" and p.prop("tier") == "" and p.prop("plan_kind") == ""
    assert p.prop("instances") == 0 and p.prop("sites") == 0
    assert p.prop("elastic_scale") is False and p.prop("os") == "windows"
    assert p.prop("status") == ""


def test_active_requires_ready_status() -> None:
    assert active(plan("asp-hot")) is None
    skip = active(plan("asp-stopped"))
    assert skip is not None and skip.reason == "not_ready" and skip.detail == "Pending"
    lowered = parse({**next(r for r in plan_rows() if r["name"] == "asp-hot"), "status": "ready"})
    assert active(lowered) is None


def test_finops_skip_is_free_and_shared_only() -> None:
    skip = finops_skip(plan("asp-free"))
    assert skip is not None and skip.reason == "no_capacity_model" and skip.detail == "Free"
    shared = parse({**next(r for r in plan_rows() if r["name"] == "asp-free"), "tier": "shared"})
    assert finops_skip(shared) is not None
    assert finops_skip(plan("asp-hot")) is None
    assert finops_skip(plan("asp-elastic")) is None
    assert finops_skip(plan("asp-stopped")) is None  # Basic has a capacity model


# --- End to end ---------------------------------------------------------------


async def test_ops_run_writes_one_row_per_hot_metric(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "asp-hot",
        {"CpuPercentage": 95.0, "MemoryPercentage": 60.0, "HttpQueueLength": 150.0},
    )
    monkeypatch.setitem(FAKE_VALUES, "asp-cold", {"CpuPercentage": 10.0, "MemoryPercentage": 30.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.skips.get("not_ready") == 1  # asp-stopped
    assert "no_capacity_model" not in summary.skips  # Free plans are still watched on Ops
    assert summary.evaluated == 5
    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []
    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [
        ("asp-hot", "cpu"),
        ("asp-hot", "http_queue"),
    ]
    cpu = next(r for r in ops if r["MetricKey"] == "cpu")
    assert cpu["MetricName"] == "CpuPercentage" and cpu["ObservedValue"] == 95.0
    assert cpu["Threshold"] == 90 and cpu["Aggregation"] == "Average"
    assert cpu["ResourceType"] == "microsoft.web/serverfarms" and cpu["Sku"] == "P1v3 x3"
    assert cpu["AssignmentGroupEmail"] == "cloud-engineering@example.com"
    queue = next(r for r in ops if r["MetricKey"] == "http_queue")
    assert queue["MetricName"] == "HttpQueueLength" and queue["Threshold"] == 100
    assert [m.name for m in metrics.requests[0]] == [
        "CpuPercentage",
        "MemoryPercentage",
        "HttpQueueLength",
    ]


async def test_finops_run_recommends_per_plan(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "asp-hot", {"CpuPercentage": 95.0, "MemoryPercentage": 60.0})
    monkeypatch.setitem(FAKE_VALUES, "asp-cold", {"CpuPercentage": 10.0, "MemoryPercentage": 30.0})
    monkeypatch.setitem(FAKE_VALUES, "asp-empty", {"CpuPercentage": 1.0, "MemoryPercentage": 5.0})
    monkeypatch.setitem(FAKE_VALUES, "asp-elastic", {"CpuPercentage": 10.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.skips.get("not_ready") == 1  # asp-stopped
    assert summary.skips.get("no_capacity_model") == 1  # asp-free
    # asp-free is skipped before the fetch: 4 of the 6 plans reach the batch call.
    assert [n for _, _, n, _ in metrics.calls] == [4]
    assert summary.evaluated == 4
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []
    assert [m.name for m in metrics.requests[0]] == ["CpuPercentage", "MemoryPercentage"]

    fin = {r["ResourceName"]: r for r in rows(tmp_path, FINOPS_TABLE)}
    assert list(fin) == ["asp-cold", "asp-empty", "asp-elastic"]  # asp-hot is not cold

    cold = fin["asp-cold"]
    assert cold["Sku"] == "P1v3 x3" and cold["MetricKey"] == "cpu" and cold["Percentile"] == 95
    assert cold["ObservedValue"] == 10.0 and cold["Threshold"] == 20
    assert cold["RecommendedSku"] == "P1v3 x2" and cold["Confidence"] == "medium"
    assert "P95 memory 30% is below 40%." in cold["Reason"]
    assert cold["Reason"].endswith(
        "2 instances cover the peak with 1.3x headroom. " + UNPRICED_NOTE
    )
    # Not priced: no Retail Prices lookups for App Service Plans.
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None
    assert cold["EstimatedMonthlySaving"] is None

    empty = fin["asp-empty"]
    assert empty["Sku"] == "S1 x1" and empty["RecommendedSku"] == "delete"
    assert "hosts no apps" in empty["Reason"] and empty["Reason"].endswith(UNPRICED_NOTE)

    elastic = fin["asp-elastic"]
    assert elastic["Sku"] == "EP1 x3" and elastic["RecommendedSku"] == ""
    assert elastic["Confidence"] == "low"  # no MemoryPercentage data fed
    assert "Memory not evaluated (no MemoryPercentage data)." in elastic["Reason"]
    assert "Elastic scale manages the instance count" in elastic["Reason"]
    assert "EP1 is already smallest allowed size in family EP" in elastic["Reason"]
    assert elastic["Reason"].endswith(UNPRICED_NOTE)


async def test_finops_busy_memory_blocks_finding(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "asp-cold", {"CpuPercentage": 10.0, "MemoryPercentage": 70.0})
    settings, clients, _ = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)
    assert summary.findings == 0 and rows(tmp_path, FINOPS_TABLE) == []
    # Every other plan had no data at all.
    assert summary.skips.get("insufficient_finops_data") == 3


def test_batch_response_maps_plan_averages() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    prefix = "/subscriptions/s1/resourceGroups/rg-web-prod/providers/Microsoft.Web/serverfarms/"
    hot, cold = prefix + "asp-hot", prefix + "asp-cold"
    cpu = MetricRequest("CpuPercentage", "Average")
    memory = MetricRequest("MemoryPercentage", "Average")
    queue = MetricRequest("HttpQueueLength", "Average")
    out = parse_batch_response(
        load_fixture("appserviceplan/metrics_batch.json"), [hot, cold], [cpu, memory, queue]
    )
    assert [p.value for p in out[hot][cpu.name]] == [94.0, 96.0, None]
    assert [p.value for p in out[hot][memory.name]] == [61.0, 63.0]
    assert [p.value for p in out[hot][queue.name]] == [120.0, 0.0]
    assert [p.value for p in out[cold][cpu.name]] == [8.0, 12.0]
    assert [p.value for p in out[cold][memory.name]] == [30.0]
    assert out[cold][queue.name] == []
