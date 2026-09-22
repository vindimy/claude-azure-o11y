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
from resource_types.servicebus import KIND, active, finops_skip, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def servicebus_rows() -> list[dict[str, object]]:
    data: list[dict[str, object]] = load_fixture("servicebus/resource_graph.json")["data"]
    return data


def fixture(name: str) -> Resource:
    return parse(next(r for r in servicebus_rows() if r["name"] == name))


class FakeInventory:
    """Serves the servicebus fixture rows; every other kind returns nothing."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == KIND:
            return [parse(r) for r in servicebus_rows()]
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


def test_parse_premium_namespace() -> None:
    ns = fixture("sb-premium")
    assert ns.kind == "servicebus" and ns.type == "microsoft.servicebus/namespaces"
    assert ns.sku == "Premium 4 MU"
    assert ns.prop("tier") == "Premium" and ns.prop("capacity") == 4
    assert ns.prop("partitions") == 1 and ns.prop("status") == "Active"
    assert ns.tag("assignment_group") == "cloud-engineering"


def test_parse_standard_namespace_has_no_size() -> None:
    ns = fixture("sb-standard")
    assert ns.sku == "Standard"
    assert ns.prop("capacity") == 0 and ns.prop("partitions") == 0


def test_parse_handles_missing_fields() -> None:
    row = {
        "id": "/x",
        "name": "n",
        "subscriptionId": "s",
        "resourceGroup": "RG",
        "location": "eastus",
        "tags": None,
        "tier": None,
        "capacity": None,
        "partitions": None,
        "status": None,
    }
    ns = parse(row)
    assert ns.tags == {} and ns.sku == "" and ns.prop("capacity") == 0
    assert active(ns) is not None and active(ns).reason == "not_active"


def test_parse_is_case_insensitive_about_the_tier_and_keeps_its_casing() -> None:
    row = {**next(r for r in servicebus_rows() if r["name"] == "sb-premium"), "tier": "premium"}
    ns = parse(row)
    assert ns.sku == "premium 4 MU" and finops_skip(ns) is None


def test_active_requires_status_active() -> None:
    assert active(fixture("sb-premium")) is None
    skip = active(fixture("sb-disabled"))
    assert skip is not None and skip.reason == "not_active" and skip.detail == "Disabled"


def test_finops_skip_is_premium_only() -> None:
    assert finops_skip(fixture("sb-premium")) is None
    skip = finops_skip(fixture("sb-standard"))
    assert skip is not None and skip.reason == "no_capacity_model" and skip.detail == "Standard"


# --- End to end ---------------------------------------------------------------


async def test_ops_run_reduces_counts_by_sum_and_depth_by_max(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "sb-premium",
        {"ThrottledRequests": 1.0, "DeadletteredMessages": 150.0, "NamespaceCpuUsage": 95.0},
    )
    monkeypatch.setitem(FAKE_VALUES, "sb-standard", {"ServerErrors": 0.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.skips.get("not_active") == 1  # sb-disabled
    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []
    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [
        ("sb-premium", "cpu"),
        ("sb-premium", "deadlettered"),
        ("sb-premium", "throttled"),
    ]
    by_key = {r["MetricKey"]: r for r in ops}
    assert by_key["throttled"]["ObservedValue"] == 60.0 and by_key["throttled"]["Threshold"] == 1
    assert by_key["throttled"]["Aggregation"] == "Total"
    assert by_key["deadlettered"]["ObservedValue"] == 150.0
    assert by_key["deadlettered"]["Aggregation"] == "Maximum"
    assert by_key["cpu"]["MetricName"] == "NamespaceCpuUsage"
    # Standard has no cpu/memory metric: applies_to keeps them out, so no no_ops_data for them.
    assert [m.name for m in metrics.requests[0]] == [
        "ThrottledRequests",
        "ServerErrors",
        "DeadletteredMessages",
        "NamespaceCpuUsage",
        "NamespaceMemoryUsage",
    ]
    # Counted per (resource, metric): sb-premium-cold has no data for all 5, sb-premium lacks
    # server_errors and memory, sb-standard lacks throttled and deadlettered (0 counts as data).
    assert summary.skips["no_ops_data"] == 9


async def test_finops_run_recommends_fewer_messaging_units(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 4 MU at 15 % CPU / 10 % memory: 4 × 0.15 × 1.3 = 0.78 -> 1 MU
    monkeypatch.setitem(
        FAKE_VALUES,
        "sb-premium-cold",
        {"NamespaceCpuUsage": 15.0, "NamespaceMemoryUsage": 10.0},
    )
    monkeypatch.setitem(
        FAKE_VALUES, "sb-premium", {"NamespaceCpuUsage": 15.0, "NamespaceMemoryUsage": 80.0}
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.skips.get("not_active") == 1  # sb-disabled
    assert summary.skips.get("no_capacity_model") == 1  # sb-standard
    assert [n for _, _, n, _ in metrics.calls] == [2]
    assert summary.evaluated == 2
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []

    # sb-premium: memory busy -> no finding; sb-premium-cold -> 1 MU
    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "sb-premium-cold" and cold["Sku"] == "Premium 4 MU"
    assert cold["MetricKey"] == "cpu" and cold["Percentile"] == 95
    assert cold["ObservedValue"] == 15.0 and cold["Threshold"] == 20
    assert cold["RecommendedSku"] == "Premium 1 MU" and cold["Confidence"] == "medium"
    assert "Namespace has 2 messaging partitions." in cold["Reason"]
    assert cold["Reason"].endswith(
        "Premium→Standard not evaluated (VNet, message size, feature checks). " + UNPRICED_NOTE
    )
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None
    assert [m.name for m in metrics.requests[0]] == ["NamespaceCpuUsage", "NamespaceMemoryUsage"]


def test_batch_response_maps_service_bus_metrics() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    prefix = (
        "/subscriptions/s1/resourceGroups/rg-messaging-prod/providers/Microsoft.ServiceBus/"
        "namespaces/"
    )
    premium, standard = prefix + "sb-premium", prefix + "sb-standard"
    requests = [
        MetricRequest("ThrottledRequests", "Total"),
        MetricRequest("ServerErrors", "Total"),
        MetricRequest("DeadletteredMessages", "Maximum"),
        MetricRequest("NamespaceCpuUsage", "Average"),
        MetricRequest("NamespaceMemoryUsage", "Average"),
    ]
    out = parse_batch_response(
        load_fixture("servicebus/metrics_batch.json"), [premium, standard], requests
    )
    assert [p.value for p in out[premium]["ThrottledRequests"]] == [2.0, 0.0, None]
    assert [p.value for p in out[premium]["ServerErrors"]] == [0.0, 1.0]
    assert [p.value for p in out[premium]["DeadletteredMessages"]] == [120.0, 121.0]
    assert [p.value for p in out[premium]["NamespaceCpuUsage"]] == [91.0, 93.0]
    assert [p.value for p in out[premium]["NamespaceMemoryUsage"]] == [40.0, 42.0]
    assert [p.value for p in out[standard]["ThrottledRequests"]] == [0.0]
    assert out[standard]["NamespaceCpuUsage"] == []  # Basic/Standard have no CPU metric
    assert out[standard]["ServerErrors"] == []  # requested, not returned
