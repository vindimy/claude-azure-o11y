"""Orchestrates one evaluation run. No SDK imports here; clients arrive through ports."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from config.models import AppConfig
from config.settings import Settings
from evaluate.suppression import SuppressionCache, SuppressionStore
from evaluate.vm import VmEvaluation, evaluate_vm
from inventory.filters import filter_vms
from metrics.batch import MetricWindow, chunk
from models import MetricPoint, Recommendation, RunSummary, Skip, VmResource
from notify.base import Notifier, ReportSink
from notify.report import ReportMeta, render_vm_report, report_relative_path
from ports import InventoryPort, MetricsPort, PricingPort
from recommend.vm import recommend_vm, with_pricing

log = logging.getLogger(__name__)
METRIC_KEY = "cpu"


@dataclass
class Clients:
    inventory: InventoryPort
    metrics: MetricsPort
    pricing: PricingPort
    notifier: Notifier
    sink: ReportSink
    suppression_store: SuppressionStore


@dataclass
class _ChunkResult:
    vms: list[VmResource]
    ops: dict[str, list[MetricPoint]]
    finops: dict[str, list[MetricPoint]]
    error: str | None = None


async def _fetch_chunk(
    metrics: MetricsPort,
    vms: list[VmResource],
    namespace: str,
    metric_name: str,
    ops_window: MetricWindow,
    finops_window: MetricWindow,
    sem: asyncio.Semaphore,
) -> _ChunkResult:
    region, sub = vms[0].location, vms[0].subscription_id
    ids = [v.id for v in vms]
    async with sem:
        try:
            ops = await metrics.query(region, sub, ids, namespace, metric_name, ops_window)
            finops = await metrics.query(region, sub, ids, namespace, metric_name, finops_window)
        except Exception as e:  # noqa: BLE001 - one bad chunk must not kill the run
            log.exception(
                "metrics chunk failed",
                extra={"region": region, "subscription": sub, "count": len(ids)},
            )
            return _ChunkResult(vms, {}, {}, error=f"{type(e).__name__}: {e}")
    return _ChunkResult(vms, ops, finops)


def _log_skip(skip: Skip, summary: RunSummary) -> None:
    summary.count_skip(skip.reason)
    log.info(
        "skipped resource",
        extra={"resource_id": skip.resource_id, "reason": skip.reason, "detail": skip.detail},
    )


async def run(
    settings: Settings, config: AppConfig, clients: Clients, now: datetime | None = None
) -> RunSummary:
    now = now or datetime.now(UTC)
    summary = RunSummary(mg_id=settings.mg_id, started_at=now)
    th = config.thresholds
    vm_cfg = th.resource_types.vm
    metric_name = vm_cfg.metrics[METRIC_KEY].metric_name

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

    # 2. Metrics, batched per (subscription, region), <= batch_size ids per call
    groups: dict[tuple[str, str], list[VmResource]] = defaultdict(list)
    for vm in filtered.kept:
        groups[(vm.subscription_id, vm.location)].append(vm)
    ops_window = MetricWindow.ops(th.windows.ops, now)
    finops_window = MetricWindow.finops(th.windows.finops, now)
    sem = asyncio.Semaphore(settings.max_concurrency)
    tasks = []
    for vms in groups.values():
        by_id = {v.id: v for v in vms}
        for ids in chunk([v.id for v in vms], settings.batch_size):
            tasks.append(
                _fetch_chunk(
                    clients.metrics,
                    [by_id[i] for i in ids],
                    vm_cfg.namespace,
                    metric_name,
                    ops_window,
                    finops_window,
                    sem,
                )
            )
    results = await asyncio.gather(*tasks)

    # 3. Evaluate
    evaluations: list[VmEvaluation] = []
    for res in results:
        if res.error:
            summary.chunk_failures += 1
            for vm in res.vms:
                _log_skip(Skip(vm.id, "chunk_failed", res.error), summary)
            continue
        for vm in res.vms:
            ev = evaluate_vm(vm, res.ops.get(vm.id, []), res.finops.get(vm.id, []), th, METRIC_KEY)
            evaluations.append(ev)
            for s in ev.skips:
                _log_skip(s, summary)
    summary.evaluated = len(evaluations)

    # 4. Ops alerts with suppression
    window = timedelta(hours=th.suppression_window_hours)
    cache = await SuppressionCache.load(clients.suppression_store, window, now=lambda: now)
    for ev in evaluations:
        if ev.hot is None:
            continue
        summary.hot_alerts += 1
        key = SuppressionCache.key(ev.resource.id, ev.hot.metric)
        if not cache.should_send(key):
            summary.hot_suppressed += 1
            log.info("hot alert suppressed", extra={"resource_id": ev.resource.id})
            continue
        try:
            await clients.notifier.send_hot(ev.hot)
            cache.mark_sent(key)
            log.warning(
                "hot alert sent",
                extra={"resource_id": ev.resource.id, "observed": ev.hot.observed},
            )
        except Exception:  # noqa: BLE001
            log.exception("notifier failed", extra={"resource_id": ev.resource.id})
    await cache.save(clients.suppression_store)

    # 5. FinOps recommendations + pricing
    recs: list[Recommendation] = []
    for ev in evaluations:
        if ev.cold is None:
            continue
        summary.cold_findings += 1
        rec = recommend_vm(ev.cold, config.vm_skus, vm_cfg.recommend)
        vm = ev.resource
        current = await clients.pricing.monthly_price(vm.location, vm.vm_size, vm.os_type)
        projected = (
            await clients.pricing.monthly_price(vm.location, rec.target_sku, vm.os_type)
            if rec.target_sku
            else None
        )
        recs.append(with_pricing(rec, current, projected))

    # 6. Report
    meta = ReportMeta(
        mg_id=settings.mg_id,
        run_at=now,
        currency=settings.pricing_currency,
        tags=th.tags,
        ignored_rg_count=summary.ignored_rg_count,
        skips=dict(summary.skips),
        excluded=summary.excluded,
        chunk_failures=summary.chunk_failures,
    )
    summary.report_location = await clients.sink.write(
        report_relative_path(now, settings.mg_id), render_vm_report(recs, meta)
    )
    log.info(
        "run complete",
        extra={
            "mg_id": summary.mg_id,
            "inventory_total": summary.inventory_total,
            "evaluated": summary.evaluated,
            "hot_alerts": summary.hot_alerts,
            "hot_suppressed": summary.hot_suppressed,
            "cold_findings": summary.cold_findings,
            "skips": summary.skips,
            "chunk_failures": summary.chunk_failures,
            "report": summary.report_location,
        },
    )
    return summary
