from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.loader import load_config
from config.models import Thresholds
from evaluate.vm import evaluate_vm, expected_points, resolve_threshold
from models import MetricPoint, VmResource

T0 = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def vm(tags: dict[str, str] | None = None, size: str = "Standard_D4s_v5") -> VmResource:
    return VmResource(
        id="/subscriptions/s1/resourceGroups/rg-a/providers/Microsoft.Compute/virtualMachines/vm1",
        name="vm1",
        subscription_id="s1",
        resource_group="rg-a",
        location="eastus",
        vm_size=size,
        os_type="Linux",
        power_state="PowerState/running",
        tags=tags or {},
    )


def points(values: list[float | None], step: timedelta) -> list[MetricPoint]:
    return [MetricPoint(T0 + i * step, v) for i, v in enumerate(values)]


@pytest.fixture
def thresholds(config_dir: Path) -> Thresholds:
    return load_config(config_dir, "mg-x").thresholds


def test_hot_when_mean_at_or_above_ops_hot(thresholds: Thresholds) -> None:
    ev = evaluate_vm(vm(), points([90.0] * 60, MIN), points([50.0] * 336, HOUR), thresholds)
    assert ev.hot is not None and ev.hot.observed == 90.0 and ev.hot.threshold == 90
    assert ev.cold is None and ev.skips == []


def test_not_hot_below_threshold(thresholds: Thresholds) -> None:
    ev = evaluate_vm(vm(), points([89.9] * 60, MIN), points([50.0] * 336, HOUR), thresholds)
    assert ev.hot is None


def test_cold_when_p95_below_finops_cold(thresholds: Thresholds) -> None:
    finops = points([5.0] * 318 + [19.0] * 18, HOUR)  # 18/336 > 5% so P95 = 19
    ev = evaluate_vm(vm(), points([10.0] * 60, MIN), finops, thresholds)
    assert ev.cold is not None
    assert ev.cold.observed_p95 == 19.0
    assert ev.cold.threshold == 20
    assert ev.cold.coverage == pytest.approx(1.0)


def test_not_cold_when_p95_at_threshold(thresholds: Thresholds) -> None:
    ev = evaluate_vm(vm(), points([10.0] * 60, MIN), points([20.0] * 336, HOUR), thresholds)
    assert ev.cold is None


def test_insufficient_finops_coverage_skips(thresholds: Thresholds) -> None:
    ev = evaluate_vm(vm(), points([10.0] * 60, MIN), points([1.0] * 100, HOUR), thresholds)
    assert ev.cold is None
    assert [s.reason for s in ev.skips] == ["insufficient_finops_data"]


def test_none_values_ignored_for_coverage(thresholds: Thresholds) -> None:
    finops = points([1.0] * 100 + [None] * 236, HOUR)
    ev = evaluate_vm(vm(), points([10.0] * 60, MIN), finops, thresholds)
    assert ev.cold is None and ev.skips[0].reason == "insufficient_finops_data"


def test_no_ops_data_skips_but_still_evaluates_cold(thresholds: Thresholds) -> None:
    ev = evaluate_vm(vm(), [], points([1.0] * 336, HOUR), thresholds)
    assert ev.hot is None
    assert ev.cold is not None
    assert [s.reason for s in ev.skips] == ["no_ops_data"]


def test_tag_override_hot_threshold(thresholds: Thresholds) -> None:
    v = vm(tags={"o11y-threshold-cpu-hot": "95"})
    ev = evaluate_vm(v, points([92.0] * 60, MIN), points([50.0] * 336, HOUR), thresholds)
    assert ev.hot is None


def test_tag_override_cold_threshold(thresholds: Thresholds) -> None:
    v = vm(tags={"o11y-threshold-cpu-cold": "5"})
    ev = evaluate_vm(v, points([10.0] * 60, MIN), points([10.0] * 336, HOUR), thresholds)
    assert ev.cold is None


def test_malformed_tag_override_falls_back(thresholds: Thresholds) -> None:
    tags = {"o11y-threshold-cpu-hot": "abc"}
    assert resolve_threshold(tags, "o11y-threshold-", "cpu", "hot", 90) == 90


def test_expected_points() -> None:
    assert expected_points(timedelta(days=14), HOUR) == 336
    assert expected_points(timedelta(minutes=60), MIN) == 60
