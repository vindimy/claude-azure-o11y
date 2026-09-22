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
from recommend.appgateway import AppGatewayRecommendRules
from resource_types.appgateway import KIND, active, enrich, finops_skip, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def appgateway_rows() -> list[dict[str, object]]:
    data: list[dict[str, object]] = load_fixture("appgateway/resource_graph.json")["data"]
    return data


def row(name: str) -> dict[str, Any]:
    return next(r for r in appgateway_rows() if r["name"] == name)


RULES = AppGatewayRecommendRules()


def parsed(row: dict[str, Any]) -> Resource:
    """What the pipeline sees: the parsed row plus the capacity model from the default rules."""
    return enrich(parse(row), RULES)


class FakeInventory:
    """Serves the appgateway fixture rows; every other kind returns nothing."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == KIND:
            return [parse(r) for r in appgateway_rows()]
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


def test_parse_autoscale_v2_gateway_keeps_the_reserved_units_out_of_the_pure_parser() -> None:
    agw = parse(row("agw-hot"))
    assert agw.kind == "appgateway" and agw.type == "microsoft.network/applicationgateways"
    assert agw.sku == "Standard_v2 autoscale 2-10"
    assert agw.prop("sku_name") == "Standard_v2" and agw.prop("tier") == "Standard_v2"
    assert agw.prop("v2") is True and agw.prop("autoscale") is True
    assert agw.prop("capacity") == 0  # null in Resource Graph for autoscale gateways
    assert agw.prop("min_capacity") == 2 and agw.prop("max_capacity") == 10
    assert agw.prop("state") == "Running"
    assert agw.tags["assignment_group"] == "cloud-engineering"
    # The CU rate is config (recommend: cu_per_instance), so the parser cannot know it.
    assert agw.prop("reserved_capacity_units") is None


def test_parse_fixed_v2_and_v1_sku_spellings() -> None:
    cold = parse(row("agw-cold"))
    assert cold.sku == "WAF_v2 x3"
    assert cold.prop("v2") is True and cold.prop("autoscale") is False
    assert cold.prop("capacity") == 3
    assert cold.prop("min_capacity") == 0 and cold.prop("max_capacity") == 0
    v1 = parse(row("agw-v1"))
    assert v1.sku == "Standard_Medium x2"
    assert v1.prop("v2") is False and v1.prop("tier") == "Standard"
    assert parse(row("agw-min0")).sku == "Standard_v2 autoscale 0-5"


def test_parse_handles_missing_fields() -> None:
    row = {
        "id": "/x",
        "name": "n",
        "subscriptionId": "s",
        "resourceGroup": "RG",
        "location": "eastus",
        "tags": None,
        "skuName": None,
        "tier": None,
        "capacity": None,
        "autoscale": None,
        "minCapacity": None,
        "maxCapacity": None,
        "state": None,
    }
    agw = parsed(row)
    assert agw.tags == {} and agw.sku == ""
    assert agw.prop("v2") is False and agw.prop("autoscale") is False
    assert agw.prop("capacity") == 0 and agw.prop("state") == ""
    assert agw.prop("reserved_capacity_units") is None


def test_v2_detection_is_case_insensitive_and_keeps_the_casing() -> None:
    """Resource Graph returns `Standard_v2` today; a differently cased tier must not become v1."""
    lowered = parse({**row("agw-cold"), "skuName": "waf_V2", "tier": "WAF_V2"})
    assert lowered.prop("v2") is True and lowered.sku == "waf_V2 x3"


# --- Enrich -------------------------------------------------------------------


def test_enrich_reserves_cu_per_instance_for_a_fixed_v2_gateway() -> None:
    agw = parsed(row("agw-cold"))
    assert agw.prop("reserved_capacity_units") == 3 * 10
    assert agw.prop("capacity") == 3 and agw.sku == "WAF_v2 x3"  # everything else untouched
    tuned = enrich(parse(row("agw-cold")), AppGatewayRecommendRules(cu_per_instance=25))
    assert tuned.prop("reserved_capacity_units") == 3 * 25


def test_enrich_uses_the_autoscale_minimum() -> None:
    assert parsed(row("agw-hot")).prop("reserved_capacity_units") == 2 * 10


def test_enrich_leaves_v1_and_autoscale_minimum_0_without_a_capacity_model() -> None:
    assert parsed(row("agw-min0")).prop("reserved_capacity_units") is None
    assert parsed(row("agw-v1")).prop("reserved_capacity_units") is None


# --- Active / skips -----------------------------------------------------------


def test_active_requires_a_running_gateway() -> None:
    assert active(parse(row("agw-hot"))) is None
    skip = active(parse(row("agw-stopped")))
    assert skip is not None and skip.reason == "not_running" and skip.detail == "Stopped"
    unknown = active(parse({**row("agw-hot"), "state": None}))
    assert unknown is not None and unknown.detail == "unknown"


def test_finops_skip_names_v1_and_autoscale_minimum_0() -> None:
    assert finops_skip(parsed(row("agw-cold"))) is None
    assert finops_skip(parsed(row("agw-hot"))) is None
    v1 = finops_skip(parsed(row("agw-v1")))
    assert v1 is not None and v1.reason == "no_capacity_model" and v1.detail == "Standard"
    min0 = finops_skip(parsed(row("agw-min0")))
    assert min0 is not None and min0.reason == "no_capacity_model"
    assert min0.detail == "autoscale minimum 0"
    no_tier = finops_skip(parsed({**row("agw-v1"), "tier": None}))
    assert no_tier is not None and no_tier.detail == "v1"


# --- End to end ---------------------------------------------------------------


async def test_ops_run_derives_failed_request_percentage_and_evaluates_v1_cpu(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "agw-hot",
        {"UnhealthyHostCount": 2.0, "FailedRequests": 6.0, "TotalRequests": 100.0},
    )
    monkeypatch.setitem(
        FAKE_VALUES,
        "agw-v1",
        {
            "UnhealthyHostCount": 0.0,
            "FailedRequests": 0.0,
            "TotalRequests": 100.0,
            "CpuUtilization": 95.0,
        },
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-1")

    assert summary.skips.get("not_running") == 1  # agw-stopped
    assert set(summary.destinations) == {OPS_TABLE}
    assert rows(tmp_path, FINOPS_TABLE) == []
    ops = rows(tmp_path, OPS_TABLE)
    by_key = {(r["ResourceName"], r["MetricKey"]): r for r in ops}
    assert set(by_key) == {
        ("agw-hot", "unhealthy_hosts"),
        ("agw-hot", "failed_requests"),
        ("agw-v1", "cpu"),
    }
    hosts = by_key[("agw-hot", "unhealthy_hosts")]
    assert hosts["ObservedValue"] == 2.0 and hosts["Threshold"] == 1
    assert hosts["MetricName"] == "UnhealthyHostCount" and hosts["Unit"] == "Count"
    failed = by_key[("agw-hot", "failed_requests")]
    assert failed["ObservedValue"] == 6.0 and failed["Threshold"] == 5
    assert failed["MetricName"] == "FailedRequestPercentage"
    assert failed["Aggregation"] == "Total (derived)"
    assert failed["Sku"] == "Standard_v2 autoscale 2-10"
    assert failed["AssignmentGroup"] == "cloud-engineering"
    cpu = by_key[("agw-v1", "cpu")]
    assert cpu["ObservedValue"] == 95.0 and cpu["MetricName"] == "CpuUtilization"
    assert [m.name for m in metrics.requests[0]] == [
        "UnhealthyHostCount",
        "FailedRequests",
        "TotalRequests",
        "CpuUtilization",
    ]


async def test_finops_run_recommends_fewer_instances_for_cold_capacity_units(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 6 consumed CU against 3 instances × 10 CU reserved: 20%.
    monkeypatch.setitem(FAKE_VALUES, "agw-cold", {"CapacityUnits": 6.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.skips.get("not_running") == 1  # agw-stopped
    assert summary.skips.get("no_capacity_model") == 2  # agw-v1, agw-min0
    assert summary.skips.get("insufficient_finops_data") == 1  # agw-hot: no data fed
    # Skipped gateways never reach the batch call: 2 of the 5 do.
    assert [n for _, _, n, _ in metrics.calls] == [2]
    assert summary.evaluated == 2
    assert set(summary.destinations) == {FINOPS_TABLE}
    assert rows(tmp_path, OPS_TABLE) == []

    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "agw-cold" and cold["Sku"] == "WAF_v2 x3"
    assert cold["MetricKey"] == "capacity" and cold["Percentile"] == 95
    assert cold["MetricName"] == "CapacityUnitsPercentage" and cold["Unit"] == "Percent"
    assert cold["ObservedValue"] == 20.0 and cold["Threshold"] == 30
    assert cold["RecommendedSku"] == "WAF_v2 x1" and cold["Confidence"] == "medium"
    assert cold["Reason"].startswith(
        "P95 capacity units 20% of 30 reserved (3 instances × 10 CU) over 14d is below 30%. "
        "Lower the instance count from 3 to 1."
    )
    assert cold["Reason"].endswith(" " + UNPRICED_NOTE)
    # Not priced: no SKU catalog, no Retail Prices lookups for Application Gateway.
    assert cold["CurrentMonthlyCost"] is None and cold["ProjectedMonthlyCost"] is None
    assert [m.name for m in metrics.requests[0]] == ["CapacityUnits"]


def test_batch_response_maps_application_gateway_metrics() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    prefix = (
        "/subscriptions/s1/resourceGroups/rg-network-prod/providers/Microsoft.Network/"
        "applicationGateways/"
    )
    hot, v1 = prefix + "agw-hot", prefix + "agw-v1"
    hosts = MetricRequest("UnhealthyHostCount", "Average")
    failed = MetricRequest("FailedRequests", "Total")
    total = MetricRequest("TotalRequests", "Total")
    cu = MetricRequest("CapacityUnits", "Average")
    cpu = MetricRequest("CpuUtilization", "Average")
    out = parse_batch_response(
        load_fixture("appgateway/metrics_batch.json"), [hot, v1], [hosts, failed, total, cu, cpu]
    )
    assert [p.value for p in out[hot][hosts.name]] == [2.0, 2.0, None]
    assert [p.value for p in out[hot][failed.name]] == [6.0, 0.0]
    assert [p.value for p in out[hot][total.name]] == [100.0, 80.0]
    assert [p.value for p in out[hot][cu.name]] == [6.0, 4.5]
    assert out[hot][cpu.name] == []  # v2 gateways have no CPU metric
    assert [p.value for p in out[v1][cpu.name]] == [95.0, 97.0]
    assert [p.value for p in out[v1][hosts.name]] == [0.0]
    assert out[v1][cu.name] == []  # v1 gateways have no capacity units
