"""End-to-end pipeline coverage for the Azure Cache for Redis (redis) resource type.

Owns its own FakeInventory (serves only redis fixtures); does not touch tests/test_pipeline.py,
which is shared with other resource types.
"""

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
from resource_types.redis import KIND, active, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def redis_rows() -> list[dict[str, object]]:
    data: list[dict[str, object]] = load_fixture("redis/resource_graph.json")["data"]
    return data


def row(name: str) -> dict[str, object]:
    return next(r for r in redis_rows() if r["name"] == name)


class FakeInventory:
    """Serves the recorded redis fixture; every other kind is empty."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        self.kinds.append(kind)
        if kind == KIND:
            return [parse(r) for r in redis_rows()]
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


def test_parse_standard_cache() -> None:
    cache = parse(row("redis-hot"))
    assert cache.kind == "redis" and cache.type == "microsoft.cache/redis"
    assert cache.sku == "Standard C1"
    assert cache.prop("tier") == "Standard" and cache.prop("family") == "C"
    assert cache.prop("capacity") == 1 and cache.prop("size") == "C1"
    assert cache.prop("state") == "Succeeded"
    assert cache.prop("shards") == 0  # null shardCount → 0
    assert cache.prop("redis_version") == "6.0"
    assert cache.tags["assignment_group"] == "cloud-engineering"


def test_parse_premium_cache_keeps_the_shard_count() -> None:
    cache = parse(row("redis-premium"))
    assert cache.sku == "Premium P2" and cache.prop("size") == "P2"
    assert cache.prop("shards") == 3


def test_parse_handles_missing_fields() -> None:
    cache = parse(
        {
            "id": "/x",
            "name": "n",
            "subscriptionId": "s",
            "resourceGroup": "RG",
            "location": "eastus",
            "tags": None,
            "tier": None,
            "family": None,
            "capacity": None,
            "state": None,
            "shards": None,
            "redisVersion": None,
        }
    )
    assert cache.tags == {} and cache.sku == ""
    assert cache.prop("tier") == "" and cache.prop("family") == "" and cache.prop("size") == ""
    assert cache.prop("capacity") == 0 and cache.prop("shards") == 0
    assert cache.prop("state") == "" and cache.prop("redis_version") == ""


def test_active_is_case_insensitive_about_the_state() -> None:
    """A lower-cased provisioning state must not skip every cache as not_ready."""
    assert active(parse(dict(row("redis-hot"), state="succeeded"))) is None
    creating = parse(row("redis-creating"))
    skip = active(creating)
    assert skip is not None and skip.reason == "not_ready" and skip.detail == "Creating"
    assert creating.prop("state") == "Creating"  # display casing is kept


# --- End to end ---------------------------------------------------------------


async def test_ops_run_writes_cpu_and_max_errors_rows(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "redis-hot",
        {"percentProcessorTime": 96.0, "usedmemorypercentage": 40.0, "errors": 3.0},
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.by_type["redis"].inventory_total == 4
    assert summary.by_type["redis"].kept == 3  # redis-creating is not ready
    assert summary.skips.get("not_ready") == 1
    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []

    ops = rows(tmp_path, OPS_TABLE)
    assert sorted(r["MetricKey"] for r in ops) == ["cpu", "errors"]
    assert {r["ResourceName"] for r in ops} == {"redis-hot"}

    cpu = next(r for r in ops if r["MetricKey"] == "cpu")
    assert cpu["MetricName"] == "percentProcessorTime" and cpu["ObservedValue"] == 96.0
    assert cpu["Threshold"] == 90 and cpu["Aggregation"] == "Average"
    assert cpu["Sku"] == "Standard C1"
    assert cpu["ResourceType"] == "microsoft.cache/redis"
    assert cpu["AssignmentGroupEmail"] == "cloud-engineering@example.com"

    errors = next(r for r in ops if r["MetricKey"] == "errors")
    assert errors["MetricName"] == "errors" and errors["Aggregation"] == "Maximum"
    assert errors["ObservedValue"] == 3.0 and errors["Threshold"] == 1  # max over the window
    assert errors["Unit"] == "Count"

    assert [(m.name, m.aggregation) for m in metrics.requests[0]] == [
        ("percentProcessorTime", "Average"),
        ("usedmemorypercentage", "Average"),
        ("serverLoad", "Average"),
        ("errors", "Maximum"),
    ]


async def test_finops_run_recommends_next_smaller_size(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES, "redis-cold", {"percentProcessorTime": 4.0, "usedmemorypercentage": 15.0}
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.skips.get("not_ready") == 1  # redis-creating
    assert summary.skips.get("insufficient_finops_data") == 2  # redis-hot, redis-premium: no data
    assert [n for _, _, n, _ in metrics.calls] == [3]
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "redis-cold" and cold["Sku"] == "Standard C2"
    assert cold["MetricKey"] == "cpu" and cold["Percentile"] == 95
    assert cold["ObservedValue"] == 4.0 and cold["Threshold"] == 20
    assert cold["RecommendedSku"] == "Standard C1"
    assert cold["Confidence"] == "medium"
    assert "P95 used memory 15% is below 30%." in cold["Reason"]
    assert cold["Reason"].endswith(UNPRICED_NOTE)
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None
    assert [m.name for m in metrics.requests[0]] == ["percentProcessorTime", "usedmemorypercentage"]


def test_batch_response_maps_redis_metrics() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    cache = (
        "/subscriptions/s1/resourceGroups/rg-cache-prod/providers/Microsoft.Cache/Redis/redis-hot"
    )
    cpu = MetricRequest("percentProcessorTime", "Average")
    memory = MetricRequest("usedmemorypercentage", "Average")
    load = MetricRequest("serverLoad", "Average")
    errors = MetricRequest("errors", "Maximum")
    requests = [cpu, memory, load, errors]
    out = parse_batch_response(load_fixture("redis/metrics_batch.json"), [cache], requests)
    assert [p.value for p in out[cache][cpu.name]] == [96.0, 97.0, None]
    assert [p.value for p in out[cache][memory.name]] == [40.0, 42.0]
    assert [p.value for p in out[cache][load.name]] == [55.0, 58.0]
    assert [p.value for p in out[cache][errors.name]] == [0.0, 3.0]
