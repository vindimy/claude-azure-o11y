from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from config.models import MetricThreshold, ResourceTypeThresholds, Thresholds
from evaluate.metric import (
    evaluate_cold,
    evaluate_hot,
    expected_points,
    observe_cold,
    reduce_window,
    resolve_threshold,
)
from models import ColdObservation, MetricPoint, Resource, Skip

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
H1 = timedelta(hours=1)
M1 = timedelta(minutes=1)


def res(tags: dict[str, str] | None = None, **props: object) -> Resource:
    return Resource(
        kind="vm",
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        name="vm1",
        type="microsoft.compute/virtualmachines",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku="Standard_D4s_v5",
        tags=tags or {},
        props={"os_type": "Linux", **props},
    )


def th(**metrics: MetricThreshold) -> Thresholds:
    return Thresholds(
        resource_types={
            "vm": ResourceTypeThresholds(
                namespace="Microsoft.Compute/virtualMachines", metrics=metrics
            )
        }
    )


def pts(values: list[float | None], step: timedelta = M1) -> list[MetricPoint]:
    return [MetricPoint(NOW - len(values) * step + i * step, v) for i, v in enumerate(values)]


CPU = MetricThreshold(metric_name="Percentage CPU", ops_hot=90, finops_cold=20)
MEM = MetricThreshold(
    metric_name="Available Memory Percentage", hot_when="below", ops_hot=10, finops_cold=70
)
THROTTLED = MetricThreshold(
    metric_name="ThrottledRequests", unit="Count", aggregation="Total", reduce="sum", ops_hot=1
)
RU_MAX = MetricThreshold(
    metric_name="NormalizedRUConsumption", aggregation="Maximum", reduce="max", ops_hot=90
)


def test_reduce_window() -> None:
    assert reduce_window([1.0, 3.0], "mean") == 2.0
    assert reduce_window([1.0, 3.0], "max") == 3.0
    assert reduce_window([1.0, 3.0], "sum") == 4.0


def test_expected_points() -> None:
    assert expected_points(timedelta(days=14), H1) == 336
    assert expected_points(timedelta(minutes=60), M1) == 60


def test_hot_above() -> None:
    hot, skips = evaluate_hot(res(), pts([95.0, 97.0]), "cpu", CPU, th(cpu=CPU))
    assert hot is not None and hot.observed == 96.0 and hot.threshold == 90 and skips == []
    assert hot.metric == "cpu" and hot.lookback_minutes == 60 and hot.threshold_source == "config"


def test_hot_at_threshold_and_below() -> None:
    hot, _ = evaluate_hot(res(), pts([90.0]), "cpu", CPU, th(cpu=CPU))
    assert hot is not None
    hot, skips = evaluate_hot(res(), pts([89.9]), "cpu", CPU, th(cpu=CPU))
    assert hot is None and skips == []


def test_hot_below_for_inverted_metric() -> None:
    hot, _ = evaluate_hot(res(), pts([8.0, 6.0]), "memory", MEM, th(memory=MEM))
    assert hot is not None and hot.observed == 7.0
    hot, _ = evaluate_hot(res(), pts([50.0]), "memory", MEM, th(memory=MEM))
    assert hot is None


def test_hot_sum_reduce_for_counts() -> None:
    hot, _ = evaluate_hot(
        res(), pts([0.0, 0.0, 1.0]), "throttled", THROTTLED, th(throttled=THROTTLED)
    )
    assert hot is not None and hot.observed == 1.0


def test_hot_max_reduce() -> None:
    hot, _ = evaluate_hot(res(), pts([10.0, 95.0, 10.0]), "ru", RU_MAX, th(ru=RU_MAX))
    assert hot is not None and hot.observed == 95.0


def test_hot_no_data_is_skip() -> None:
    hot, skips = evaluate_hot(res(), pts([None]), "cpu", CPU, th(cpu=CPU))
    assert hot is None and skips == [Skip(res().id, "no_ops_data", "no datapoints in ops window")]


def test_hot_tag_override_and_malformed_tag() -> None:
    hot, _ = evaluate_hot(
        res({"O11Y-Threshold-CPU-Hot": "75"}), pts([80.0]), "cpu", CPU, th(cpu=CPU)
    )
    assert hot is not None and hot.threshold == 75 and hot.threshold_source == "tag"
    assert (
        resolve_threshold({"o11y-threshold-cpu-hot": "x"}, "o11y-threshold-", "cpu", "hot", 90)
        == 90
    )


def test_observe_cold_above() -> None:
    obs = observe_cold(res(), pts([5.0] * 336, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, ColdObservation)
    assert obs.cold and obs.percentile == 95 and obs.value == 5.0 and obs.coverage == 1.0
    assert obs.median == 5.0 and obs.threshold == 20


def test_observe_cold_p95_nearest_rank() -> None:
    values = [5.0] * 318 + [19.0] * 18  # 18/336 > 5 % so P95 = 19
    obs = observe_cold(res(), pts(values, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, ColdObservation) and obs.value == 19.0 and obs.cold


def test_observe_cold_at_threshold_is_not_cold() -> None:
    obs = observe_cold(res(), pts([20.0] * 336, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, ColdObservation) and not obs.cold


def test_observe_cold_below_flips_percentile() -> None:
    values = [80.0] * 300 + [40.0] * 36  # P5 of available memory is 40 → not cold
    obs = observe_cold(res(), pts(values, H1), "memory", MEM, th(memory=MEM), H1)
    assert isinstance(obs, ColdObservation)
    assert obs.percentile == 5 and obs.value == 40.0 and not obs.cold
    obs = observe_cold(res(), pts([80.0] * 336, H1), "memory", MEM, th(memory=MEM), H1)
    assert isinstance(obs, ColdObservation) and obs.cold


def test_observe_cold_insufficient_coverage_counts_only_valid_points() -> None:
    obs = observe_cold(res(), pts([5.0] * 100, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, Skip) and obs.reason == "insufficient_finops_data"
    assert obs.detail.startswith("cpu: coverage 30%")
    obs = observe_cold(res(), pts([1.0] * 100 + [None] * 236, H1), "cpu", CPU, th(cpu=CPU), H1)
    assert isinstance(obs, Skip)


def test_observe_cold_tag_override() -> None:
    obs = observe_cold(
        res({"o11y-threshold-cpu-cold": "5"}), pts([6.0] * 336, H1), "cpu", CPU, th(cpu=CPU), H1
    )
    assert isinstance(obs, ColdObservation) and not obs.cold and obs.threshold_source == "tag"


def test_evaluate_cold_requires_primary_cold() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([50.0] * 336, H1), "memory": pts([80.0] * 336, H1)}
    finding, skips = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is None and skips == []


def test_evaluate_cold_secondary_not_cold_blocks() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([5.0] * 336, H1), "memory": pts([20.0] * 336, H1)}
    finding, _ = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is None


def test_evaluate_cold_both_cold() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([5.0] * 336, H1), "memory": pts([80.0] * 336, H1)}
    finding, _ = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is not None
    assert [o.metric for o in finding.observations] == ["cpu", "memory"]
    mem = finding.observation("memory")
    assert mem is not None and mem.value == 80.0 and mem.percentile == 5
    assert finding.lookback_days == 14 and finding.coverage == 1.0


def test_evaluate_cold_secondary_without_data_does_not_block() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"cpu": pts([5.0] * 336, H1), "memory": []}
    finding, skips = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is not None and finding.metric == "cpu" and finding.observed == 5.0
    assert finding.observation("memory") is None and skips == []


def test_evaluate_cold_primary_without_data_is_skip() -> None:
    t = th(cpu=CPU)
    finding, skips = evaluate_cold(res(), {"cpu": []}, t.resource_types["vm"], t, H1)
    assert finding is None and skips[0].reason == "insufficient_finops_data"


def test_evaluate_cold_primary_is_first_applicable_metric() -> None:
    t = th(cpu=CPU, memory=MEM)
    series = {"memory": pts([80.0] * 336, H1)}  # cpu did not apply to this resource
    finding, _ = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is not None and finding.metric == "memory" and finding.percentile == 5


def test_evaluate_cold_no_finops_metric_in_series() -> None:
    t = th(cpu=CPU)
    assert evaluate_cold(res(), {}, t.resource_types["vm"], t, H1) == (None, [])


def test_evaluate_cold_collects_inputs_latest_value() -> None:
    prov = MetricThreshold(metric_name="ProvisionedThroughput", unit="Count", aggregation="Maximum")
    t = th(cpu=CPU, provisioned=prov)
    series = {"cpu": pts([5.0] * 336, H1), "provisioned": pts([1000.0, 2000.0, None], H1)}
    finding, _ = evaluate_cold(res(), series, t.resource_types["vm"], t, H1)
    assert finding is not None and finding.inputs == {"provisioned": 2000.0}


def test_applies_to() -> None:
    m = MetricThreshold(
        metric_name="dtu_consumption_percent", applies_to={"purchasing_model": ["dtu"]}, ops_hot=90
    )
    assert not m.applies(res())
    assert m.applies(res(purchasing_model="DTU"))
    assert MetricThreshold(metric_name="x", ops_hot=1).applies(res())
    flag = MetricThreshold(metric_name="x", applies_to={"hyperscale": ["false"]}, ops_hot=1)
    assert flag.applies(res(hyperscale=False)) and not flag.applies(res(hyperscale=True))


def test_type_thresholds_partition_metrics() -> None:
    cfg = ResourceTypeThresholds(
        namespace="n",
        metrics={
            "a": MetricThreshold(metric_name="a", ops_hot=1),
            "b": MetricThreshold(metric_name="b", finops_cold=1),
            "c": MetricThreshold(metric_name="c"),
            "d": MetricThreshold(metric_name="d", ops_hot=1, finops_cold=1),
        },
    )
    assert list(cfg.ops_metrics()) == ["a", "d"]
    assert list(cfg.finops_metrics()) == ["b", "d"]
    assert list(cfg.input_metrics()) == ["c"]
    assert cfg.primary_finops_key() == "b"


def test_metric_needs_a_name_and_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        MetricThreshold.model_validate({"ops_hot": 1})
    with pytest.raises(ValidationError):
        MetricThreshold.model_validate({"metric_name": "x", "bogus": 1})
    with pytest.raises(ValidationError):
        MetricThreshold.model_validate({"metric_name": "x", "hot_when": "sideways"})
