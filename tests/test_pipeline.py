from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from config.loader import load_config
from config.settings import Settings
from evaluate.suppression import LocalSuppressionStore
from inventory.vms import parse_vm_row
from metrics.batch import MetricWindow
from models import MetricPoint, Scope, VmResource
from notify.console import ConsoleNotifier
from notify.sinks import LocalReportSink
from pipeline import Clients, run
from tests.conftest import load_fixture

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


class FakeInventory:
    async def list_vms(self, scope: Scope) -> list[VmResource]:
        rows = (
            load_fixture("resource_graph_vms_page1.json")["data"]
            + load_fixture("resource_graph_vms_page2.json")["data"]
        )
        return [parse_vm_row(r) for r in rows]


class FakeMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metric_name: str,
        window: MetricWindow,
    ) -> dict[str, list[MetricPoint]]:
        self.calls.append((region, subscription_id, len(resource_ids)))
        n = int((window.end - window.start) / window.granularity)
        out: dict[str, list[MetricPoint]] = {}
        for rid in resource_ids:
            name = rid.rsplit("/", 1)[1]
            if name == "vm-hot":
                vals = [96.0] * n
            elif name == "vm-cold":
                vals = [4.0] * n
            else:
                vals = []
            out[rid] = [
                MetricPoint(window.start + i * window.granularity, v) for i, v in enumerate(vals)
            ]
        return out


class FakePricing:
    async def monthly_price(self, region: str, sku: str, os_type: str) -> Decimal | None:
        prices = {"standard_d8s_v5": Decimal("560.64"), "standard_d4s_v5": Decimal("280.32")}
        return prices.get(sku.lower())


class Boom:
    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metric_name: str,
        window: MetricWindow,
    ) -> dict[str, list[MetricPoint]]:
        raise RuntimeError("boom")


def make(
    tmp_path: Path, config_dir: Path
) -> tuple[Settings, Clients, FakeMetrics, ConsoleNotifier]:
    settings = Settings(mg_id="mg-prod", dry_run=True, output_dir=tmp_path, config_dir=config_dir)
    cfg = load_config(config_dir, "mg-prod")
    metrics = FakeMetrics()
    notifier = ConsoleNotifier(cfg.thresholds.tags)
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        notifier=notifier,
        sink=LocalReportSink(tmp_path),
        suppression_store=LocalSuppressionStore(tmp_path / "sup.json"),
    )
    return settings, clients, metrics, notifier


async def test_end_to_end_dry_run(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics, notifier = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, now=NOW)

    assert summary.inventory_total == 6
    assert summary.evaluated == 3
    assert summary.hot_alerts == 1 and summary.hot_suppressed == 0
    assert [a.resource.name for a in notifier.sent] == ["vm-hot"]
    assert summary.cold_findings == 1
    assert summary.ignored_rg_count == 1
    assert summary.skips == {
        "ignored_rg": 1,
        "excluded_by_tag": 1,
        "not_running": 1,
        "no_ops_data": 1,
        "insufficient_finops_data": 1,
    }
    assert [v.name for v in summary.excluded] == ["vm-excluded"]
    # one region/subscription -> 1 chunk x 2 windows
    assert metrics.calls == [("eastus", "s1", 3), ("eastus", "s1", 3)]

    report = Path(summary.report_location or "")
    assert report.name == "virtual-machines.md" and report.parent.name == "mg-prod"
    md = report.read_text()
    assert "| vm-cold |" in md and "| Standard_D4s_v5 |" in md and "280.32" in md
    assert "Ignored resource groups: 1" in md


async def test_second_run_suppresses_hot_alert(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _, notifier = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    await run(settings, cfg, clients, now=NOW)
    second = await run(settings, cfg, clients, now=NOW + timedelta(minutes=15))
    assert second.hot_alerts == 1 and second.hot_suppressed == 1
    assert len(notifier.sent) == 1


async def test_metrics_failure_is_counted_not_fatal(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, _, _ = make(tmp_path, config_dir)
    clients.metrics = Boom()
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, now=NOW)
    assert summary.chunk_failures == 1
    assert summary.skips.get("chunk_failed") == 3
    assert summary.report_location
