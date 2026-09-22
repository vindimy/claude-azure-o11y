"""Orchestrates one evaluation run (Ops or FinOps). No SDK imports; clients arrive through ports.

One run loops over the enabled resource types (thresholds config order, narrowed by
RESOURCE_TYPES). Each type is inventoried, filtered, fetched in batches, evaluated, and turned
into rows; the rows of every type are written to the mode's table in one call at the end.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from config.models import AppConfig, MetricThreshold, ResourceTypeThresholds
from config.settings import Settings
from errors import PermissionMissing
from evaluate.metric import evaluate_cold, evaluate_hot
from inventory.filters import filter_resources
from metrics.batch import MetricWindow, chunk
from metrics.derive import RunMode, requests_for, resolve_series
from models import MetricRequest, Resource, RunSummary, Series, Skip
from notify.base import FindingsSink
from notify.findings import (
    FINOPS_TABLE,
    OPS_TABLE,
    MetricContext,
    Row,
    RunContext,
    finops_row,
    ops_row,
)
from ports import InventoryPort, MetricsPort, PricingPort
from resource_types import TYPES
from resource_types.registry import ResourceTypeSpec

log = logging.getLogger(__name__)

# Appended to the reason of every FinOps row of a type PricingPort cannot price (`SPEC.priced`),
# so FinOps sees why the cost columns are empty. Recommenders themselves never mention pricing.
UNPRICED_NOTE = "Pricing not implemented for this resource type."

__all__ = ["Clients", "FindingsWriteFailed", "RunMode", "UNPRICED_NOTE", "run"]


@dataclass
class Clients:
    inventory: InventoryPort
    metrics: MetricsPort
    pricing: PricingPort
    findings: FindingsSink


@dataclass
class _ChunkResult:
    resources: list[Resource]
    series: dict[str, Series] = field(default_factory=dict)
    error: str | None = None


async def _fetch_chunk(
    metrics: MetricsPort,
    resources: list[Resource],
    namespace: str,
    requests: list[MetricRequest],
    window: MetricWindow,
    sem: asyncio.Semaphore,
) -> _ChunkResult:
    region, sub = resources[0].location, resources[0].subscription_id
    ids = [r.id for r in resources]
    async with sem:
        try:
            series = await metrics.query(region, sub, ids, namespace, requests, window)
        except Exception as e:  # noqa: BLE001 - one bad chunk must not kill the run
            log.exception(
                "metrics chunk failed",
                extra={"region": region, "subscription": sub, "count": len(ids)},
            )
            return _ChunkResult(resources, error=f"{type(e).__name__}: {e}")
    return _ChunkResult(resources, series)


class FindingsWriteFailed(RuntimeError):
    """Raised after the run completes when the findings table write failed."""


async def _write(
    sink: FindingsSink, table: str, rows: list[dict[str, Any]], summary: RunSummary
) -> None:
    if not rows:
        return
    try:
        summary.destinations[table] = await sink.write(table, rows)
    except PermissionMissing:
        raise
    except Exception:  # noqa: BLE001 - logged with context, then surfaced after the summary
        summary.write_failures += 1
        log.exception("findings write failed", extra={"table": table, "rows": len(rows)})
        return
    summary.rows_written = len(rows)
    log.info("findings written", extra={"table": table, "rows": len(rows)})


def _log_skip(skip: Skip, summary: RunSummary) -> None:
    summary.count_skip(skip.reason)
    log.info(
        "skipped resource",
        extra={"resource_id": skip.resource_id, "reason": skip.reason, "detail": skip.detail},
    )


def _metric_ctx(
    type_cfg: ResourceTypeThresholds,
    key: str,
    cfg: MetricThreshold,
    spec: ResourceTypeSpec,
    window: MetricWindow,
    granularity: str,
) -> MetricContext:
    if spec.metric_source == "inventory":
        label = "computed"
    elif cfg.derive:
        label = f"{cfg.aggregation} (derived)"
    else:
        label = cfg.aggregation
    return MetricContext(type_cfg.namespace, key, cfg, label, window.start, window.end, granularity)


async def _fetch_all(
    clients: Clients,
    settings: Settings,
    resources: list[Resource],
    namespace: str,
    requests: list[MetricRequest],
    window: MetricWindow,
) -> list[_ChunkResult]:
    groups: dict[tuple[str, str], list[Resource]] = defaultdict(list)
    for r in resources:
        groups[(r.subscription_id, r.location)].append(r)
    sem = asyncio.Semaphore(settings.max_concurrency)
    tasks = []
    for group in groups.values():
        by_id = {r.id: r for r in group}
        for ids in chunk([r.id for r in group], settings.batch_size):
            batch = [by_id[i] for i in ids]
            tasks.append(_fetch_chunk(clients.metrics, batch, namespace, requests, window, sem))
    return list(await asyncio.gather(*tasks))


async def _run_type(
    kind: str,
    settings: Settings,
    config: AppConfig,
    clients: Clients,
    mode: RunMode,
    now: datetime,
    run_ctx: RunContext,
    summary: RunSummary,
) -> list[Row]:
    spec = TYPES[kind]
    th = config.thresholds
    type_cfg = th.resource_types[kind]
    ts = summary.for_type(kind)

    # 1. Inventory + filters. `enrich` adds the facts that need the type's rules (config), which
    # the pure parser cannot see.
    rules = config.rules[kind]
    resources = [
        spec.enrich(r, rules) for r in await clients.inventory.list_resources(kind, settings.scope)
    ]
    ts.inventory_total = len(resources)
    summary.inventory_total += len(resources)
    filtered = filter_resources(
        resources, th.tags, config.ignore.patterns_for(settings.mg_id), spec.active
    )
    ts.kept = len(filtered.kept)
    summary.ignored_rg_count += filtered.ignored_rg_count
    summary.excluded.extend(filtered.excluded)
    for s in filtered.skips:
        _log_skip(s, summary)
    log.info(
        "inventory",
        extra={
            "kind": kind,
            "total": len(resources),
            "kept": len(filtered.kept),
            "scope": settings.scope.kind,
        },
    )

    # 2. Metrics for this mode's window only, batched per (subscription, region).
    # Resources FinOps cannot size are dropped here, before the fetch: they cost no metrics call
    # and are not counted as evaluated.
    kept = list(filtered.kept)
    if mode == "finops":
        sizeable: list[Resource] = []
        for r in kept:
            skip = spec.finops_skip(r)
            if skip is None:
                sizeable.append(r)
            else:
                _log_skip(skip, summary)
        kept = sizeable
    if mode == "ops":
        granularity = type_cfg.granularity.ops or th.windows.ops.granularity
        window = MetricWindow.ops(th.windows.ops, now, type_cfg.granularity.ops)
    else:
        granularity = type_cfg.granularity.finops or th.windows.finops.granularity
        window = MetricWindow.finops(th.windows.finops, now, type_cfg.granularity.finops)
    requests = requests_for(type_cfg, mode)
    raw: dict[str, Series] = {}
    if spec.metric_source == "monitor" and requests:
        for res in await _fetch_all(clients, settings, kept, type_cfg.namespace, requests, window):
            if res.error:
                ts.chunk_failures += 1
                summary.chunk_failures += 1
                for r in res.resources:
                    _log_skip(Skip(r.id, "chunk_failed", res.error), summary)
                continue
            raw.update(res.series)
        evaluable = [r for r in kept if r.id in raw]
    else:
        evaluable = kept

    # 3. Evaluate this mode's side and build rows
    rows: list[Row] = []
    for r in evaluable:
        ts.evaluated += 1
        summary.evaluated += 1
        series = resolve_series(
            r, type_cfg, raw.get(r.id, {}), mode, window.granularity, now, spec.metric_source
        )
        if mode == "ops":
            for key, points in series.items():
                cfg = type_cfg.metrics[key]
                hot, skips = evaluate_hot(r, points, key, cfg, th)
                for s in skips:
                    _log_skip(s, summary)
                if hot is None:
                    continue
                rows.append(
                    ops_row(
                        hot, run_ctx, _metric_ctx(type_cfg, key, cfg, spec, window, granularity)
                    )
                )
                log.warning(
                    "ops finding",
                    extra={"resource_id": r.id, "metric": key, "observed": hot.observed},
                )
            continue
        cold, skips = evaluate_cold(r, series, type_cfg, th, window.granularity)
        for s in skips:
            _log_skip(s, summary)
        if cold is None or spec.recommend is None:
            continue
        rec = spec.recommend(cold, config)
        if spec.priced:
            os_type = str(r.prop("os_type", ""))
            current = await clients.pricing.monthly_price(r.location, r.sku, os_type)
            projected = (
                await clients.pricing.monthly_price(r.location, rec.target_sku, os_type)
                if rec.target_sku
                else None
            )
            rec = rec.with_pricing(current, projected)
        else:
            rec = rec.with_note(UNPRICED_NOTE)
        cfg = type_cfg.metrics[cold.metric]
        rows.append(
            finops_row(
                rec,
                run_ctx,
                _metric_ctx(type_cfg, cold.metric, cfg, spec, window, granularity),
            )
        )
    ts.findings = len(rows)
    return rows


async def run(
    settings: Settings,
    config: AppConfig,
    clients: Clients,
    mode: RunMode,
    now: datetime | None = None,
    run_id: str | None = None,
) -> RunSummary:
    now = now or datetime.now(UTC)
    summary = RunSummary(mg_id=settings.mg_id, mode=mode, started_at=now)
    th = config.thresholds
    run_ctx = RunContext(
        run_id=run_id or str(uuid.uuid4()),
        mg_id=settings.mg_id,
        run_at=now,
        tags=th.tags,
        assignment_groups=config.assignment_groups,
        currency=settings.pricing_currency,
    )

    rows: list[Row] = []
    for kind in settings.resource_type_list(list(th.resource_types)):
        try:
            rows.extend(
                await _run_type(kind, settings, config, clients, mode, now, run_ctx, summary)
            )
        except PermissionMissing:
            raise
        except Exception:  # noqa: BLE001 - one type must not kill the run
            summary.type_failures += 1
            log.exception("resource type failed", extra={"kind": kind})
    summary.findings = len(rows)

    # 4. One write per run
    await _write(clients.findings, OPS_TABLE if mode == "ops" else FINOPS_TABLE, rows, summary)

    log.info(
        "run complete",
        extra={
            "mg_id": summary.mg_id,
            "mode": mode,
            "run_id": run_ctx.run_id,
            "inventory_total": summary.inventory_total,
            "evaluated": summary.evaluated,
            "findings": summary.findings,
            "rows_written": summary.rows_written,
            "write_failures": summary.write_failures,
            "skips": summary.skips,
            "ignored_rg_count": summary.ignored_rg_count,
            "excluded": [r.id for r in summary.excluded],
            "chunk_failures": summary.chunk_failures,
            "type_failures": summary.type_failures,
            "by_type": {k: vars(v) for k, v in summary.by_type.items()},
            "destinations": summary.destinations,
        },
    )
    if summary.write_failures:
        raise FindingsWriteFailed(f"{mode} findings write failed; see logs")
    return summary
