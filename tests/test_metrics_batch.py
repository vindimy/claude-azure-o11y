from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from config.models import FinopsWindow, OpsWindow
from metrics.batch import MetricWindow, chunk, parse_batch_response
from models import MetricRequest
from tests.conftest import load_fixture

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
PREFIX = "/subscriptions/s1/resourceGroups/rg-app-prod/providers/Microsoft.Compute/virtualMachines/"
CPU = MetricRequest("Percentage CPU", "Average")
MEM = MetricRequest("Available Memory Percentage", "Average")


def test_parse_maps_by_resource_id_case_insensitively() -> None:
    hot, cold, missing = PREFIX + "vm-hot", PREFIX + "vm-cold", PREFIX + "vm-nodata"
    out = parse_batch_response(load_fixture("metrics_batch_cpu.json"), [hot, cold, missing], [CPU])
    assert [p.value for p in out[hot][CPU.name]] == [95.0, 97.5, None]
    assert out[hot][CPU.name][0].timestamp == datetime(2026, 9, 15, 11, 0, tzinfo=UTC)
    assert [p.value for p in out[cold][CPU.name]] == [3.0]
    assert out[missing] == {CPU.name: []}


def test_parse_several_metrics_and_aggregations() -> None:
    hot, cold = PREFIX + "vm-hot", PREFIX + "vm-cold"
    payload = load_fixture("metrics_batch_cpu.json")
    payload["values"][0]["value"].append(
        {
            "name": {"value": "Available Memory Percentage"},
            "unit": "Percent",
            "timeseries": [
                {
                    "data": [
                        {"timeStamp": "2026-09-15T11:00:00Z", "average": 12.5, "maximum": 20.0},
                    ]
                }
            ],
        }
    )
    out = parse_batch_response(payload, [hot, cold], [CPU, MetricRequest(MEM.name, "Maximum")])
    assert [p.value for p in out[hot][MEM.name]] == [20.0]
    assert out[cold][MEM.name] == []  # requested, not returned for this resource
    assert [p.value for p in out[cold][CPU.name]] == [3.0]


def test_parse_ignores_unrequested_metric_names() -> None:
    hot = PREFIX + "vm-hot"
    out = parse_batch_response(load_fixture("metrics_batch_cpu.json"), [hot], [MEM])
    assert out[hot] == {MEM.name: []}


def test_parse_falls_back_to_request_order_without_resourceid() -> None:
    payload = load_fixture("metrics_batch_cpu.json")
    for v in payload["values"]:
        del v["resourceid"]
    out = parse_batch_response(payload, ["/a", "/b"], [CPU])
    assert len(out["/a"][CPU.name]) == 3 and len(out["/b"][CPU.name]) == 1


def test_multiple_timeseries_are_concatenated_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dimension-split metrics would duplicate timestamps; make that visible in the logs."""
    hot = PREFIX + "vm-hot"
    payload = load_fixture("metrics_batch_cpu.json")
    metric = payload["values"][0]["value"][0]
    metric["timeseries"].append({"data": [{"timeStamp": "2026-09-15T11:00:00Z", "average": 1.0}]})
    with caplog.at_level(logging.WARNING, logger="metrics.batch"):
        out = parse_batch_response(payload, [hot], [CPU])
    assert [p.value for p in out[hot][CPU.name]] == [95.0, 97.5, None, 1.0]
    [record] = [r for r in caplog.records if "more than one timeseries" in r.message]
    assert record.metric == "Percentage CPU" and record.timeseries == 2


def test_ops_window_and_override() -> None:
    w = MetricWindow.ops(OpsWindow(lookback_minutes=60, granularity="PT1M"), NOW)
    assert w.start == NOW - timedelta(minutes=60) and w.end == NOW
    assert w.granularity == timedelta(minutes=1) and w.aggregation == "Average"
    w = MetricWindow.ops(OpsWindow(), NOW, "PT5M")
    assert w.granularity == timedelta(minutes=5)


def test_finops_window_and_override() -> None:
    w = MetricWindow.finops(FinopsWindow(lookback_days=14, granularity="PT1H"), NOW)
    assert w.start == NOW - timedelta(days=14) and w.granularity == timedelta(hours=1)
    assert MetricWindow.finops(FinopsWindow(), NOW, "P1D").granularity == timedelta(days=1)


def test_chunk() -> None:
    ids = [str(i) for i in range(120)]
    assert chunk(ids, 50) == [ids[:50], ids[50:100], ids[100:]]
    assert chunk([], 50) == []
