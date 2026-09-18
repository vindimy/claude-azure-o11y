"""Pure VM threshold evaluation. No I/O, no SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import isodate

from config.models import Thresholds
from evaluate.percentile import percentile
from models import ColdFinding, HotAlert, MetricPoint, Skip, ThresholdSource, VmResource


@dataclass(frozen=True)
class VmEvaluation:
    resource: VmResource
    hot: HotAlert | None
    cold: ColdFinding | None
    skips: list[Skip] = field(default_factory=list)


def resolve_threshold_with_source(
    tags: dict[str, str], prefix: str, metric_key: str, kind: str, default: float
) -> tuple[float, ThresholdSource]:
    """Tag `<prefix><metric>-<kind>` (e.g. o11y-threshold-cpu-hot) overrides config.

    A malformed value falls back to the configured default.
    """
    wanted = f"{prefix}{metric_key}-{kind}".lower()
    for k, v in tags.items():
        if k.lower() == wanted:
            try:
                return float(v), "tag"
            except ValueError:
                return default, "config"
    return default, "config"


def resolve_threshold(
    tags: dict[str, str], prefix: str, metric_key: str, kind: str, default: float
) -> float:
    return resolve_threshold_with_source(tags, prefix, metric_key, kind, default)[0]


def expected_points(lookback: timedelta, granularity: timedelta) -> int:
    return int(lookback / granularity)


def _valid(points: list[MetricPoint]) -> list[float]:
    return [p.average for p in points if p.average is not None]


def evaluate_hot(
    vm: VmResource, points: list[MetricPoint], thresholds: Thresholds, metric_key: str = "cpu"
) -> tuple[HotAlert | None, list[Skip]]:
    """Ops side: the mean over the Ops window at or above the hot threshold."""
    cfg = thresholds.resource_types.vm.metrics[metric_key]
    values = _valid(points)
    if not values:
        return None, [Skip(vm.id, "no_ops_data", "no datapoints in ops window")]
    threshold, source = resolve_threshold_with_source(
        vm.tags, thresholds.tags.threshold_prefix, metric_key, "hot", cfg.ops_hot
    )
    observed = sum(values) / len(values)
    if observed < threshold:
        return None, []
    return (
        HotAlert(
            resource=vm,
            metric=metric_key,
            observed=round(observed, 2),
            threshold=threshold,
            lookback_minutes=thresholds.windows.ops.lookback_minutes,
            threshold_source=source,
        ),
        [],
    )


def evaluate_cold(
    vm: VmResource, points: list[MetricPoint], thresholds: Thresholds, metric_key: str = "cpu"
) -> tuple[ColdFinding | None, list[Skip]]:
    """FinOps side: the percentile over the FinOps window below the cold threshold."""
    cfg = thresholds.resource_types.vm.metrics[metric_key]
    fin = thresholds.windows.finops
    values = _valid(points)
    expected = expected_points(
        timedelta(days=fin.lookback_days), isodate.parse_duration(fin.granularity)
    )
    coverage = len(values) / expected if expected else 0.0
    if coverage < fin.min_coverage:
        detail = f"coverage {coverage:.0%} < {fin.min_coverage:.0%}"
        return None, [Skip(vm.id, "insufficient_finops_data", detail)]
    threshold, source = resolve_threshold_with_source(
        vm.tags, thresholds.tags.threshold_prefix, metric_key, "cold", cfg.finops_cold
    )
    p95 = percentile(values, fin.percentile)
    if p95 is None or p95 >= threshold:
        return None, []
    return (
        ColdFinding(
            resource=vm,
            metric=metric_key,
            observed_p95=round(p95, 2),
            threshold=threshold,
            lookback_days=fin.lookback_days,
            coverage=round(coverage, 3),
            threshold_source=source,
        ),
        [],
    )


def evaluate_vm(
    vm: VmResource,
    ops_points: list[MetricPoint],
    finops_points: list[MetricPoint],
    thresholds: Thresholds,
    metric_key: str = "cpu",
) -> VmEvaluation:
    hot, hot_skips = evaluate_hot(vm, ops_points, thresholds, metric_key)
    cold, cold_skips = evaluate_cold(vm, finops_points, thresholds, metric_key)
    return VmEvaluation(resource=vm, hot=hot, cold=cold, skips=hot_skips + cold_skips)
