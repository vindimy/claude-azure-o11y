from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from config.models import MetricThreshold, ResourceTypeThresholds
from metrics.derive import bytes_per_second_percent, ratio_percent, requests_for, resolve_series
from models import MetricPoint, MetricRequest, Resource

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
M1 = timedelta(minutes=1)


def p(values: list[float | None]) -> list[MetricPoint]:
    return [MetricPoint(NOW + i * M1, v) for i, v in enumerate(values)]


def res(**props: object) -> Resource:
    return Resource("x", "/r/1", "r1", "t", "s", "rg", "eastus", "sku", {}, dict(props))


def test_ratio_percent_pairs_by_timestamp() -> None:
    out = ratio_percent(p([50.0, None, 75.0]), p([100.0, 100.0, 0.0]))
    assert [pt.value for pt in out] == [50.0, None, None]
    assert out[0].timestamp == NOW
    assert ratio_percent(p([1.0]), []) == [MetricPoint(NOW, None)]


def test_bytes_per_second_percent() -> None:
    # 60 MB per minute on a 1 MB/s capacity = 100 %
    out = bytes_per_second_percent(p([60 * 1024 * 1024, None]), 60.0, 1024 * 1024)
    assert [pt.value for pt in out] == [100.0, None]
    assert [pt.value for pt in bytes_per_second_percent(p([1.0]), 60.0, 0)] == [None]


SQLMI = ResourceTypeThresholds(
    namespace="Microsoft.Sql/managedInstances",
    metrics={
        "cpu": MetricThreshold(metric_name="avg_cpu_percent", ops_hot=90, finops_cold=20),
        "storage": MetricThreshold(
            metric_name="storage_percent",
            derive="ratio_percent",
            inputs=["storage_space_used_mb", "reserved_storage_mb"],
            ops_hot=90,
        ),
        "dtu": MetricThreshold(
            metric_name="dtu_consumption_percent", applies_to={"model": ["dtu"]}, ops_hot=90
        ),
        "cold_only": MetricThreshold(metric_name="x", finops_cold=1),
        "input_only": MetricThreshold(metric_name="y", aggregation="Maximum"),
    },
)


def test_resolve_series_raw_derived_and_applies() -> None:
    raw = {
        "avg_cpu_percent": p([10.0]),
        "storage_space_used_mb": p([40.0]),
        "reserved_storage_mb": p([80.0]),
        "dtu_consumption_percent": p([1.0]),
    }
    out = resolve_series(res(model="vcore"), SQLMI, raw, "ops", M1, NOW)
    assert list(out) == ["cpu", "storage"]
    assert [pt.value for pt in out["storage"]] == [50.0]
    out = resolve_series(res(model="dtu"), SQLMI, raw, "ops", M1, NOW)
    assert list(out) == ["cpu", "storage", "dtu"]


def test_resolve_series_finops_includes_inputs() -> None:
    out = resolve_series(res(), SQLMI, {"y": p([3.0])}, "finops", M1, NOW)
    assert list(out) == ["cpu", "cold_only", "input_only"]
    assert out["cpu"] == [] and [pt.value for pt in out["input_only"]] == [3.0]


def test_requests_for_dedupes_and_expands_inputs() -> None:
    assert requests_for(SQLMI, "ops") == [
        MetricRequest("avg_cpu_percent", "Average"),
        MetricRequest("storage_space_used_mb", "Average"),
        MetricRequest("reserved_storage_mb", "Average"),
        MetricRequest("dtu_consumption_percent", "Average"),
    ]
    assert requests_for(SQLMI, "finops") == [
        MetricRequest("avg_cpu_percent", "Average"),
        MetricRequest("x", "Average"),
        MetricRequest("y", "Maximum"),
    ]


def test_resolve_series_from_inventory_props() -> None:
    cfg = ResourceTypeThresholds(
        namespace="Microsoft.Network/virtualNetworks",
        metrics={
            "subnet_ip": MetricThreshold(
                metric_name="SubnetIpUtilization", inputs=["utilization_percent"], ops_hot=80
            )
        },
    )
    out = resolve_series(res(utilization_percent=91.5), cfg, {}, "ops", M1, NOW, "inventory")
    assert out == {"subnet_ip": [MetricPoint(NOW, 91.5)]}
    assert resolve_series(res(), cfg, {}, "ops", M1, NOW, "inventory") == {"subnet_ip": []}
    assert requests_for(cfg, "ops") == [MetricRequest("SubnetIpUtilization", "Average")]


EH = ResourceTypeThresholds(
    namespace="Microsoft.EventHub/namespaces",
    metrics={
        "ingress": MetricThreshold(
            metric_name="IncomingBytes",
            aggregation="Total",
            derive="bytes_per_second_percent",
            inputs=["IncomingBytes"],
            capacity_prop="capacity_bytes_per_second",
            finops_cold=30,
        )
    },
)


def test_resolve_series_capacity_prop() -> None:
    raw = {"IncomingBytes": p([30 * 1024 * 1024])}
    out = resolve_series(res(capacity_bytes_per_second=1024 * 1024), EH, raw, "finops", M1, NOW)
    assert [pt.value for pt in out["ingress"]] == [50.0]
    assert resolve_series(res(), EH, raw, "finops", M1, NOW) == {}  # no capacity: dropped


def test_unknown_derive_fails_loudly() -> None:
    cfg = ResourceTypeThresholds(
        namespace="n",
        metrics={"k": MetricThreshold(metric_name="m", derive="nope", inputs=["m"], ops_hot=1)},
    )
    with pytest.raises(ValueError, match="unknown derive"):
        resolve_series(res(), cfg, {}, "ops", M1, NOW)
