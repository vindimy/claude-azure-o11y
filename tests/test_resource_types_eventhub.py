from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from config.loader import load_config
from config.settings import Settings
from metrics.batch import parse_batch_response
from models import MetricRequest, Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import UNPRICED_NOTE, Clients, run
from recommend.eventhub import EventHubRecommendRules
from resource_types.eventhub import BYTES_PER_MB, KIND, enrich, finops_skip, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def eventhub_rows() -> list[dict[str, object]]:
    data: list[dict[str, object]] = load_fixture("eventhub/resource_graph.json")["data"]
    return data


RULES = EventHubRecommendRules()


def parsed(row: dict[str, Any]) -> Resource:
    """What the pipeline sees: the parsed row plus the capacity model from the default rules."""
    return enrich(parse(row), RULES)


class FakeInventory:
    """Serves the eventhub fixture rows; every other kind returns nothing."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == KIND:
            return [parse(r) for r in eventhub_rows()]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types=KIND,
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


# --- Parser -----------------------------------------------------------------


def test_parse_standard_namespace_keeps_the_capacity_out_of_the_pure_parser() -> None:
    row = next(r for r in eventhub_rows() if r["name"] == "eh-standard")
    ns = parse(row)
    assert ns.kind == "eventhub" and ns.type == "microsoft.eventhub/namespaces"
    assert ns.sku == "Standard 4 TU"
    assert ns.prop("tier") == "Standard" and ns.prop("capacity") == 4
    assert ns.prop("auto_inflate") is True and ns.prop("max_tu") == 8
    # The unit rate is config (recommend: mb_per_tu / mb_per_pu), so the parser cannot know it.
    assert ns.prop("capacity_bytes_per_second") is None


def test_enrich_computes_capacity_bytes_per_second_from_the_configured_rates() -> None:
    row = next(r for r in eventhub_rows() if r["name"] == "eh-standard")
    ns = parsed(row)
    # Azure quotes TU ingress as 1 decimal MB/s, so a MB here is 1_000_000 bytes.
    assert ns.prop("capacity_bytes_per_second") == 4 * 1 * 1_000_000
    assert BYTES_PER_MB == 1_000_000
    assert ns.prop("capacity") == 4 and ns.sku == "Standard 4 TU"  # everything else untouched
    tuned = enrich(parse(row), EventHubRecommendRules(mb_per_tu=2))
    assert tuned.prop("capacity_bytes_per_second") == 4 * 2 * BYTES_PER_MB


def test_enrich_premium_namespace_uses_the_pu_rate() -> None:
    row = next(r for r in eventhub_rows() if r["name"] == "eh-premium")
    ns = parsed(row)
    assert ns.sku == "Premium 1 PU"
    assert ns.prop("capacity_bytes_per_second") == 1 * 5 * BYTES_PER_MB


def test_enrich_dedicated_or_zero_capacity_namespace_has_no_capacity_model() -> None:
    row = next(r for r in eventhub_rows() if r["name"] == "eh-dedicated")
    ns = parsed(row)
    assert ns.sku == "Dedicated"
    assert ns.prop("capacity_bytes_per_second") is None
    standard = next(r for r in eventhub_rows() if r["name"] == "eh-standard")
    assert parsed({**standard, "capacity": 0}).prop("capacity_bytes_per_second") is None


def test_parse_handles_missing_fields() -> None:
    row = {
        "id": "/x",
        "name": "n",
        "subscriptionId": "s",
        "resourceGroup": "RG",
        "location": "eastus",
        "tags": None,
        "tier": None,
        "capacity": None,
        "autoInflate": None,
        "maxTu": None,
    }
    ns = parsed(row)
    assert ns.tags == {} and ns.sku == "" and ns.prop("capacity_bytes_per_second") is None


def test_parse_is_case_insensitive_about_the_tier_and_keeps_its_casing() -> None:
    """Resource Graph returns title case today; a lower-cased tier must not mislabel the units."""
    row = {
        "id": "/x",
        "name": "n",
        "subscriptionId": "s",
        "resourceGroup": "RG",
        "location": "eastus",
        "tags": None,
        "tier": "premium",
        "capacity": 1,
        "autoInflate": None,
        "maxTu": None,
    }
    ns = parsed(row)
    assert ns.sku == "premium 1 PU"
    assert ns.prop("capacity_bytes_per_second") == 5 * BYTES_PER_MB
    assert finops_skip(parse({**row, "tier": "dedicated"})) is not None
    assert parse({**row, "tier": "dedicated"}).sku == "dedicated"


def test_finops_skip_is_dedicated_only() -> None:
    dedicated = parse(next(r for r in eventhub_rows() if r["name"] == "eh-dedicated"))
    standard = parse(next(r for r in eventhub_rows() if r["name"] == "eh-standard"))
    skip = finops_skip(dedicated)
    assert skip is not None and skip.reason == "no_capacity_model"
    assert finops_skip(standard) is None


# --- End to end ---------------------------------------------------------------


async def test_ops_run_uses_sum_reduction_for_throttled_requests(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "eh-standard", {"ThrottledRequests": 1.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []
    ops = rows(tmp_path, OPS_TABLE)
    assert [r["ResourceName"] for r in ops] == ["eh-standard"]
    [hot] = ops
    assert hot["MetricKey"] == "throttled" and hot["ObservedValue"] == 60.0
    assert hot["MetricName"] == "ThrottledRequests" and hot["Threshold"] == 1
    assert hot["Aggregation"] == "Total"
    assert [m.name for m in metrics.requests[0]] == ["ThrottledRequests", "NamespaceCpuUsage"]


async def test_finops_run_recommends_fewer_units_for_cold_ingress(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 20% of a 4 TU (4 decimal MB/s) namespace's capacity, as a PT1H total: 0.2 * 4e6 B/s * 3600s.
    monkeypatch.setitem(FAKE_VALUES, "eh-standard", {"IncomingBytes": 2_880_000_000.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.skips.get("no_capacity_model") == 1  # eh-dedicated
    assert summary.skips.get("insufficient_finops_data") == 1  # eh-premium: no data fed
    # eh-dedicated is skipped before the fetch: 2 of the 3 namespaces reach the batch call.
    assert [n for _, _, n, _ in metrics.calls] == [2]
    assert summary.evaluated == 2
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "eh-standard" and cold["Sku"] == "Standard 4 TU"
    assert cold["MetricKey"] == "ingress" and cold["Percentile"] == 95
    assert cold["ObservedValue"] == 20.0 and cold["Threshold"] == 30
    assert cold["RecommendedSku"] == "Standard 2 TU" and cold["Confidence"] == "medium"
    assert "already at the smallest size" not in cold["Reason"]
    assert cold["Reason"].endswith(
        "Standard→Basic not evaluated (needs capture/consumer-group/retention checks). "
        + UNPRICED_NOTE
    )
    # Not priced: no SKU catalog, no Retail Prices lookups for Event Hubs.
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None
    assert [m.name for m in metrics.requests[0]] == ["IncomingBytes"]


def test_batch_response_maps_event_hubs_totals() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    prefix = (
        "/subscriptions/s1/resourceGroups/rg-messaging-prod/providers/Microsoft.EventHub/"
        "namespaces/"
    )
    standard, premium = prefix + "eh-standard", prefix + "eh-premium"
    throttled = MetricRequest("ThrottledRequests", "Total")
    ingress = MetricRequest("IncomingBytes", "Total")
    cpu = MetricRequest("NamespaceCpuUsage", "Average")
    out = parse_batch_response(
        load_fixture("eventhub/metrics_batch.json"), [standard, premium], [throttled, ingress, cpu]
    )
    assert [p.value for p in out[standard][throttled.name]] == [1.0, 0.0, None]
    assert [p.value for p in out[standard][ingress.name]] == [48_000_000.0, 60_000_000.0]
    assert out[standard][cpu.name] == []  # Basic/Standard have no CPU metric
    assert [p.value for p in out[premium][cpu.name]] == [91.0, 93.0]
    assert [p.value for p in out[premium][ingress.name]] == [300_000_000.0]
