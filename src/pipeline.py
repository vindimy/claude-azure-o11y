"""Orchestrates one evaluation run (Ops or FinOps). No SDK imports; clients arrive through ports."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from config.models import AppConfig
from config.settings import Settings
from errors import PermissionMissing
from evaluate.vm import evaluate_cold, evaluate_hot
from inventory.filters import filter_vms
from metrics.batch import MetricWindow, chunk
from models import MetricPoint, RunSummary, Skip, VmResource
from notify.base import FindingsSink
from notify.findings import (
    FINOPS_TABLE,
    OPS_TABLE,
    MetricContext,
    RunContext,
    finops_row,
    ops_row,
)
from ports import InventoryPort, MetricsPort, PricingPort
from recommend.vm import recommend_vm, with_pricing

log = logging.getLogger(__name__)
METRIC_KEY = "cpu"

# One timer trigger per mode: Ops every few minutes, FinOps once a day.
RunMode = Literal["ops", "finops"]


@dataclass
class Clients:
    inventory: InventoryPort
    metrics: MetricsPort
    pricing: PricingPort
    findings: FindingsSink


@dataclass
class _ChunkResult:
    vms: list[VmResource]
    points: dict[str, list[MetricPoint]]
    error: str | None = None


async def _fetch_chunk(
    metrics: MetricsPort,
    vms: list[VmResource],
    namespace: str,
    metric_name: str,
    window: MetricWindow,
    sem: asyncio.Semaphore,
) -> _ChunkResult:
    region, sub = vms[0].location, vms[0].subscription_id
    ids = [v.id for v in vms]
    async with sem:
        try:
            points = await metrics.query(region, sub, ids, namespace, metric_name, window)
        except Exception as e:  # noqa: BLE001 - one bad chunk must not kill the run
            log.exception(
                "metrics chunk failed",
                extra={"region": region, "subscription": sub, "count": len(ids)},
            )
            return _ChunkResult(vms, {}, error=f"{type(e).__name__}: {e}")
    return _ChunkResult(vms, points)


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
    vm_cfg = th.resource_types.vm
    metric_cfg = vm_cfg.metrics[METRIC_KEY]

    # 1. Inventory + filters
    all_vms = await clients.inventory.list_vms(settings.scope)
    summary.inventory_total = len(all_vms)
    filtered = filter_vms(all_vms, th.tags, config.ignore.patterns_for(settings.mg_id))
    summary.ignored_rg_count = filtered.ignored_rg_count
    summary.excluded = filtered.excluded
    for s in filtered.skips:
        _log_skip(s, summary)
    log.info(
        "inventory",
        extra={"total": len(all_vms), "kept": len(filtered.kept), "scope": settings.scope.kind},
    )

    # 2. Metrics for this mode's window only, batched per (subscription, region)
    if mode == "ops":
        window = MetricWindow.ops(th.windows.ops, now)
        aggregation = th.windows.ops.aggregation
    else:
        window = MetricWindow.finops(th.windows.finops, now)
        aggregation = th.windows.finops.aggregation
    groups: dict[tuple[str, str], list[VmResource]] = defaultdict(list)
    for vm in filtered.kept:
        groups[(vm.subscription_id, vm.location)].append(vm)
    sem = asyncio.Semaphore(settings.max_concurrency)
    tasks = []
    for vms in groups.values():
        by_id = {v.id: v for v in vms}
        for ids in chunk([v.id for v in vms], settings.batch_size):
            batch = [by_id[i] for i in ids]
            tasks.append(
                _fetch_chunk(
                    clients.metrics, batch, vm_cfg.namespace, metric_cfg.metric_name, window, sem
                )
            )
    results = await asyncio.gather(*tasks)

    run_ctx = RunContext(
        run_id=run_id or str(uuid.uuid4()),
        mg_id=settings.mg_id,
        run_at=now,
        tags=th.tags,
        assignment_groups=config.assignment_groups,
        currency=settings.pricing_currency,
    )
    metric_ctx = MetricContext(
        vm_cfg.namespace, METRIC_KEY, metric_cfg, aggregation, window.start, window.end
    )

    # 3. Evaluate this mode's side and build rows
    rows: list[dict[str, Any]] = []
    for res in results:
        if res.error:
            summary.chunk_failures += 1
            for vm in res.vms:
                _log_skip(Skip(vm.id, "chunk_failed", res.error), summary)
            continue
        for vm in res.vms:
            summary.evaluated += 1
            points = res.points.get(vm.id, [])
            if mode == "ops":
                hot, skips = evaluate_hot(vm, points, th, METRIC_KEY)
                if hot is not None:
                    rows.append(ops_row(hot, run_ctx, metric_ctx))
                    log.warning(
                        "ops finding", extra={"resource_id": vm.id, "observed": hot.observed}
                    )
            else:
                cold, skips = evaluate_cold(vm, points, th, METRIC_KEY)
                if cold is not None:
                    rec = recommend_vm(cold, config.vm_skus, vm_cfg.recommend)
                    current = await clients.pricing.monthly_price(
                        vm.location, vm.vm_size, vm.os_type
                    )
                    projected = (
                        await clients.pricing.monthly_price(vm.location, rec.target_sku, vm.os_type)
                        if rec.target_sku
                        else None
                    )
                    priced = with_pricing(rec, current, projected)
                    rows.append(finops_row(priced, run_ctx, metric_ctx, th.windows.finops))
            for s in skips:
                _log_skip(s, summary)
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
            "excluded": [v.id for v in summary.excluded],
            "chunk_failures": summary.chunk_failures,
            "destinations": summary.destinations,
        },
    )
    if summary.write_failures:
        raise FindingsWriteFailed(f"{mode} findings write failed; see logs")
    return summary
