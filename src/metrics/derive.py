"""Turn raw metric series into the per-metric-key series the evaluators consume. Pure.

A metric key maps to a raw Azure metric (`metric_name`), a derivation over raw metrics
(`derive` + `inputs`), or, for inventory-sourced types, a resource prop (`inputs[0]`).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from config.models import MetricThreshold, ResourceTypeThresholds
from models import MetricPoint, MetricRequest, Resource, Series

RunMode = Literal["ops", "finops"]
MetricSource = Literal["monitor", "inventory"]


def ratio_percent(
    numerator: list[MetricPoint], denominator: list[MetricPoint]
) -> list[MetricPoint]:
    """numerator / denominator × 100, paired by timestamp; None where either side is missing."""
    den = {p.timestamp: p.value for p in denominator}
    out: list[MetricPoint] = []
    for p in numerator:
        d = den.get(p.timestamp)
        if p.value is None or d is None or d <= 0:
            out.append(MetricPoint(p.timestamp, None))
        else:
            out.append(MetricPoint(p.timestamp, round(p.value / d * 100, 2)))
    return out


def bytes_per_second_percent(
    totals: list[MetricPoint], interval_seconds: float, capacity_bytes_per_second: float
) -> list[MetricPoint]:
    """Per-interval byte totals as a percentage of a bytes-per-second capacity."""
    if interval_seconds <= 0 or capacity_bytes_per_second <= 0:
        return [MetricPoint(p.timestamp, None) for p in totals]
    return [
        MetricPoint(
            p.timestamp,
            None
            if p.value is None
            else round(p.value / interval_seconds / capacity_bytes_per_second * 100, 2),
        )
        for p in totals
    ]


def wanted_metrics(type_cfg: ResourceTypeThresholds, mode: RunMode) -> dict[str, MetricThreshold]:
    """The metric keys one run mode evaluates (plus recommender inputs in FinOps)."""
    if mode == "ops":
        return type_cfg.ops_metrics()
    return {**type_cfg.finops_metrics(), **type_cfg.input_metrics()}


def requests_for(type_cfg: ResourceTypeThresholds, mode: RunMode) -> list[MetricRequest]:
    """Every raw metric name the batch call must fetch for this type and mode, de-duplicated."""
    seen: dict[str, MetricRequest] = {}
    for cfg in wanted_metrics(type_cfg, mode).values():
        names = cfg.inputs if cfg.derive else [cfg.metric_name]
        for n in names:
            seen.setdefault(n, MetricRequest(n, cfg.aggregation))
    return list(seen.values())


def resolve_series(
    resource: Resource,
    type_cfg: ResourceTypeThresholds,
    raw: Series,
    mode: RunMode,
    granularity: timedelta,
    now: datetime,
    source: MetricSource = "monitor",
) -> Series:
    """Series keyed by metric key for the metrics that apply to this resource in this mode."""
    out: Series = {}
    for key, cfg in wanted_metrics(type_cfg, mode).items():
        if not cfg.applies(resource):
            continue
        if source == "inventory":
            value = resource.prop(cfg.inputs[0]) if cfg.inputs else None
            out[key] = [MetricPoint(now, float(value))] if value is not None else []
            continue
        if cfg.derive is None:
            out[key] = raw.get(cfg.metric_name, [])
            continue
        inputs = [raw.get(name, []) for name in cfg.inputs]
        if cfg.derive == "ratio_percent":
            out[key] = ratio_percent(inputs[0], inputs[1])
        elif cfg.derive == "bytes_per_second_percent":
            capacity = resource.prop(cfg.capacity_prop or "")
            if not capacity:
                continue
            out[key] = bytes_per_second_percent(
                inputs[0], granularity.total_seconds(), float(capacity)
            )
        else:
            raise ValueError(f"unknown derive {cfg.derive!r} for metric {key}")
    return out
