"""Pure VM threshold evaluation. No I/O, no SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import isodate

from config.models import Thresholds
from evaluate.percentile import percentile
from models import ColdFinding, HotAlert, MetricPoint, Skip, VmResource


@dataclass(frozen=True)
class VmEvaluation:
    resource: VmResource
    hot: HotAlert | None
    cold: ColdFinding | None
    skips: list[Skip] = field(default_factory=list)


def resolve_threshold(
    tags: dict[str, str], prefix: str, metric_key: str, kind: str, default: float
) -> float:
    """Tag `<prefix><metric>-<kind>` (e.g. o11y-threshold-cpu-hot) overrides config.

    A malformed value falls back to the configured default.
    """
    wanted = f"{prefix}{metric_key}-{kind}".lower()
    for k, v in tags.items():
        if k.lower() == wanted:
            try:
                return float(v)
            except ValueError:
                return default
    return default


def expected_points(lookback: timedelta, granularity: timedelta) -> int:
    return int(lookback / granularity)


def _valid(points: list[MetricPoint]) -> list[float]:
    return [p.average for p in points if p.average is not None]


def evaluate_vm(
    vm: VmResource,
    ops_points: list[MetricPoint],
    finops_points: list[MetricPoint],
    thresholds: Thresholds,
    metric_key: str = "cpu",
) -> VmEvaluation:
    cfg = thresholds.resource_types.vm.metrics[metric_key]
    prefix = thresholds.tags.threshold_prefix
    skips: list[Skip] = []

    hot: HotAlert | None = None
    ops_values = _valid(ops_points)
    if ops_values:
        hot_threshold = resolve_threshold(vm.tags, prefix, metric_key, "hot", cfg.ops_hot)
        observed = sum(ops_values) / len(ops_values)
        if observed >= hot_threshold:
            hot = HotAlert(
                resource=vm,
                metric=metric_key,
                observed=round(observed, 2),
                threshold=hot_threshold,
                lookback_minutes=thresholds.windows.ops.lookback_minutes,
            )
    else:
        skips.append(Skip(vm.id, "no_ops_data", "no datapoints in ops window"))

    cold: ColdFinding | None = None
    fin = thresholds.windows.finops
    finops_values = _valid(finops_points)
    expected = expected_points(
        timedelta(days=fin.lookback_days), isodate.parse_duration(fin.granularity)
    )
    coverage = len(finops_values) / expected if expected else 0.0
    if coverage < fin.min_coverage:
        skips.append(
            Skip(
                vm.id,
                "insufficient_finops_data",
                f"coverage {coverage:.0%} < {fin.min_coverage:.0%}",
            )
        )
    else:
        cold_threshold = resolve_threshold(vm.tags, prefix, metric_key, "cold", cfg.finops_cold)
        p95 = percentile(finops_values, fin.percentile)
        if p95 is not None and p95 < cold_threshold:
            cold = ColdFinding(
                resource=vm,
                metric=metric_key,
                observed_p95=round(p95, 2),
                threshold=cold_threshold,
                lookback_days=fin.lookback_days,
                coverage=round(coverage, 3),
            )
    return VmEvaluation(resource=vm, hot=hot, cold=cold, skips=skips)
