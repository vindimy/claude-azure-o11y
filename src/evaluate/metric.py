"""Threshold evaluation for any metric of any resource. Pure; no I/O, no SDK."""

from __future__ import annotations

from datetime import timedelta

from config.models import MetricThreshold, ResourceTypeThresholds, Thresholds
from evaluate.percentile import percentile
from models import (
    ColdFinding,
    ColdObservation,
    HotAlert,
    MetricPoint,
    Resource,
    Series,
    Skip,
    ThresholdSource,
)


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
    return [p.value for p in points if p.value is not None]


def reduce_window(values: list[float], how: str) -> float:
    if how == "max":
        return max(values)
    if how == "sum":
        return sum(values)
    return sum(values) / len(values)


def evaluate_hot(
    resource: Resource, points: list[MetricPoint], key: str, cfg: MetricThreshold, th: Thresholds
) -> tuple[HotAlert | None, list[Skip]]:
    """Ops side: the window reduction at or beyond the hot threshold (direction per hot_when)."""
    assert cfg.ops_hot is not None, f"{key} has no ops_hot"
    values = _valid(points)
    if not values:
        return None, [Skip(resource.id, "no_ops_data", "no datapoints in ops window")]
    threshold, source = resolve_threshold_with_source(
        resource.tags, th.tags.threshold_prefix, key, "hot", cfg.ops_hot
    )
    observed = round(reduce_window(values, cfg.reduce), 2)
    breached = observed >= threshold if cfg.hot_when == "above" else observed <= threshold
    if not breached:
        return None, []
    return (
        HotAlert(
            resource=resource,
            metric=key,
            observed=observed,
            threshold=threshold,
            lookback_minutes=th.windows.ops.lookback_minutes,
            threshold_source=source,
        ),
        [],
    )


def observe_cold(
    resource: Resource,
    points: list[MetricPoint],
    key: str,
    cfg: MetricThreshold,
    th: Thresholds,
    granularity: timedelta,
) -> ColdObservation | Skip:
    """FinOps side for one metric: the percentile over the window against the cold threshold.

    For `hot_when: below` metrics the low percentile (100 - P) is taken, because peak usage of an
    inverted metric is its minimum, and the metric is cold when that value is *above* the threshold.
    """
    assert cfg.finops_cold is not None, f"{key} has no finops_cold"
    fin = th.windows.finops
    values = _valid(points)
    expected = expected_points(timedelta(days=fin.lookback_days), granularity)
    coverage = len(values) / expected if expected else 0.0
    if coverage < fin.min_coverage:
        detail = f"{key}: coverage {coverage:.0%} < {fin.min_coverage:.0%}"
        return Skip(resource.id, "insufficient_finops_data", detail)
    threshold, source = resolve_threshold_with_source(
        resource.tags, th.tags.threshold_prefix, key, "cold", cfg.finops_cold
    )
    p = fin.percentile if cfg.hot_when == "above" else 100 - fin.percentile
    value = percentile(values, p)
    median = percentile(values, 50)
    assert value is not None and median is not None
    cold = value < threshold if cfg.hot_when == "above" else value > threshold
    return ColdObservation(
        metric=key,
        percentile=p,
        value=round(value, 2),
        median=round(median, 2),
        threshold=threshold,
        cold=cold,
        coverage=round(coverage, 3),
        threshold_source=source,
    )


def _latest(points: list[MetricPoint]) -> float | None:
    for p in reversed(points):
        if p.value is not None:
            return p.value
    return None


def evaluate_cold(
    resource: Resource,
    series: Series,
    type_cfg: ResourceTypeThresholds,
    th: Thresholds,
    granularity: timedelta,
) -> tuple[ColdFinding | None, list[Skip]]:
    """A finding needs the primary FinOps metric cold and every other covered one cold too.

    `series` holds only the metrics that apply to this resource, in config order, so the primary
    metric is the first applicable one. A secondary metric with insufficient coverage does not
    block the finding; the recommender lowers confidence and says so in Reason.
    """
    finops = {k: m for k, m in type_cfg.finops_metrics().items() if k in series}
    primary = next(iter(finops), None)
    if primary is None:
        return None, []
    observations: list[ColdObservation] = []
    for key, cfg in finops.items():
        obs = observe_cold(resource, series[key], key, cfg, th, granularity)
        if isinstance(obs, Skip):
            if key == primary:
                return None, [obs]
            continue
        if not obs.cold:
            return None, []
        observations.append(obs)
    head = observations[0]
    inputs = {
        key: v
        for key in type_cfg.input_metrics()
        if key in series and (v := _latest(series[key])) is not None
    }
    return (
        ColdFinding(
            resource=resource,
            metric=head.metric,
            observed=head.value,
            percentile=head.percentile,
            threshold=head.threshold,
            lookback_days=th.windows.finops.lookback_days,
            coverage=head.coverage,
            threshold_source=head.threshold_source,
            observations=tuple(observations),
            inputs=inputs,
        ),
        [],
    )
