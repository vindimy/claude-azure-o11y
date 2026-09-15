from __future__ import annotations

from datetime import UTC, datetime, timedelta

from config.models import FinopsWindow, OpsWindow
from metrics.batch import MetricWindow, chunk, parse_batch_response
from tests.conftest import load_fixture

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
PREFIX = "/subscriptions/s1/resourceGroups/rg-app-prod/providers/Microsoft.Compute/virtualMachines/"


def test_parse_maps_by_resource_id_case_insensitively() -> None:
    hot, cold, missing = PREFIX + "vm-hot", PREFIX + "vm-cold", PREFIX + "vm-nodata"
    out = parse_batch_response(load_fixture("metrics_batch_cpu.json"), [hot, cold, missing])
    assert [p.average for p in out[hot]] == [95.0, 97.5, None]
    assert out[hot][0].timestamp == datetime(2026, 9, 15, 11, 0, tzinfo=UTC)
    assert [p.average for p in out[cold]] == [3.0]
    assert out[missing] == []


def test_parse_falls_back_to_request_order_without_resourceid() -> None:
    payload = load_fixture("metrics_batch_cpu.json")
    for v in payload["values"]:
        del v["resourceid"]
    out = parse_batch_response(payload, ["/a", "/b"])
    assert len(out["/a"]) == 3 and len(out["/b"]) == 1


def test_ops_window() -> None:
    w = MetricWindow.ops(OpsWindow(lookback_minutes=60, granularity="PT1M"), NOW)
    assert w.start == NOW - timedelta(minutes=60) and w.end == NOW
    assert w.granularity == timedelta(minutes=1) and w.aggregation == "Average"


def test_finops_window() -> None:
    w = MetricWindow.finops(FinopsWindow(lookback_days=14, granularity="PT1H"), NOW)
    assert w.start == NOW - timedelta(days=14) and w.granularity == timedelta(hours=1)


def test_chunk() -> None:
    ids = [str(i) for i in range(120)]
    assert chunk(ids, 50) == [ids[:50], ids[50:100], ids[100:]]
    assert chunk([], 50) == []
