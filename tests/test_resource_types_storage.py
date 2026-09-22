from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.loader import load_config
from config.settings import Settings
from metrics.batch import parse_batch_response
from models import MetricRequest, Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import UNPRICED_NOTE, Clients, run
from resource_types.storage import KIND, active, finops_skip, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
THROTTLE_FILTER = "ResponseType eq 'ServerBusyError' or ResponseType eq 'ClientThrottlingError'"
GIB = 2**30


def accounts() -> list[Resource]:
    return [parse(row) for row in load_fixture("storage/resource_graph.json")["data"]]


def account(name: str) -> Resource:
    return next(a for a in accounts() if a.name == name)


class FakeInventory:
    """Serves the recorded storage account fixture; every other kind is empty."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        return accounts() if kind == KIND else []


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


def test_parse_reads_sku_tier_and_access_tier_into_sku_and_props() -> None:
    hot = account("st-hot")
    assert hot.type == "microsoft.storage/storageaccounts"
    assert hot.sku == "Standard_LRS Hot"
    assert hot.prop("sku_name") == "Standard_LRS" and hot.prop("tier") == "Standard"
    assert hot.prop("account_kind") == "StorageV2" and hot.prop("access_tier") == "Hot"
    assert hot.prop("state") == "Succeeded" and hot.prop("hns") is False
    assert hot.prop("tierable") is True
    assert hot.subscription_id == "s1" and hot.resource_group == "rg-app-prod"
    assert hot.tags == {
        "car_id": "200",
        "owner": "alice@example.com",
        "assignment_group": "cloud-engineering",
    }
    assert account("st-cold").prop("hns") is True


def test_parse_tolerates_missing_access_tier_and_hns() -> None:
    premium = account("st-premium")
    assert premium.sku == "Premium_LRS" and premium.prop("access_tier") == ""
    assert premium.prop("tier") == "Premium" and premium.prop("account_kind") == "BlockBlobStorage"
    v1 = account("st-v1")
    assert v1.sku == "Standard_LRS" and v1.prop("hns") is False
    bare = parse(
        {
            "id": "/x",
            "name": "x",
            "subscriptionId": "s",
            "resourceGroup": "rg",
            "location": "eastus",
        }
    )
    assert bare.sku == "" and bare.prop("state") == "" and bare.prop("tierable") is False


@pytest.mark.parametrize(
    ("name", "tierable"),
    [
        ("st-hot", True),
        ("st-cold", True),
        ("st-cool", False),  # already Cool
        ("st-premium", False),  # Premium tier, no access tier
        ("st-v1", False),  # general-purpose v1 has no access tiers
        ("st-creating", True),  # tierable once provisioned; `active` handles readiness
    ],
)
def test_tierable_needs_standard_v2_or_blob_kind_in_the_hot_tier(name: str, tierable: bool) -> None:
    assert account(name).prop("tierable") is tierable


def test_tierable_compares_case_insensitively() -> None:
    row = {**load_fixture("storage/resource_graph.json")["data"][0]}
    row.update({"kind": "blobstorage", "tier": "STANDARD", "accessTier": "hot"})
    assert parse(row).prop("tierable") is True


def test_active_requires_a_succeeded_provisioning_state() -> None:
    assert active(account("st-hot")) is None
    skip = active(account("st-creating"))
    assert skip is not None
    assert skip.reason == "not_ready" and skip.detail == "Creating"


def test_finops_skip_names_the_tier_or_kind_that_blocks_tiering() -> None:
    assert finops_skip(account("st-hot")) is None
    assert finops_skip(account("st-cold")) is None
    cool = finops_skip(account("st-cool"))
    assert cool is not None and cool.reason == "not_tierable" and cool.detail == "Cool"
    premium = finops_skip(account("st-premium"))
    assert premium is not None and premium.reason == "not_tierable" and premium.detail == "Premium"
    v1 = finops_skip(account("st-v1"))
    assert v1 is not None and v1.reason == "not_tierable" and v1.detail == "Storage"


async def test_ops_run_flags_low_availability_and_throttling(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "st-hot", {"Availability": 99.5, "Transactions": 1.0})
    settings, clients, metrics = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "ops", now=NOW)

    # The throttled key filters Transactions by ResponseType; the request carries the OData filter.
    assert metrics.requests[0] == [
        MetricRequest("Availability", "Average"),
        MetricRequest("Transactions", "Total", THROTTLE_FILTER, "ResponseType"),
    ]
    assert metrics.granularities == [timedelta(minutes=1)]
    assert summary.inventory_total == 6 and summary.evaluated == 5
    assert summary.skips["not_ready"] == 1 and "not_tierable" not in summary.skips
    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [
        ("st-hot", "availability"),
        ("st-hot", "throttled"),
    ]
    availability = next(r for r in ops if r["MetricKey"] == "availability")
    assert availability["MetricName"] == "Availability" and availability["Aggregation"] == "Average"
    assert availability["ObservedValue"] == 99.5 and availability["Threshold"] == 99.9
    assert availability["MetricNamespace"] == "Microsoft.Storage/storageAccounts"
    assert availability["ResourceType"] == "microsoft.storage/storageaccounts"
    assert availability["Sku"] == "Standard_LRS Hot" and availability["Location"] == "eastus"
    assert availability["AssignmentGroupEmail"] == "cloud-engineering@example.com"
    throttled = next(r for r in ops if r["MetricKey"] == "throttled")
    assert throttled["MetricName"] == "Transactions" and throttled["Aggregation"] == "Total"
    assert throttled["ObservedValue"] == 60.0 and throttled["Threshold"] == 1  # sum of 60 × 1
    assert throttled["Unit"] == "Count"
    assert rows(tmp_path, FINOPS_TABLE) == []


async def test_finops_run_recommends_the_cool_tier_and_skips_untierable_accounts(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES, "st-cold", {"Transactions": 100.0, "UsedCapacity": 500.0 * GIB}
    )
    monkeypatch.setitem(
        FAKE_VALUES, "st-hot", {"Transactions": 5000.0, "UsedCapacity": 500.0 * GIB}
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)

    # FinOps reads Transactions unfiltered; UsedCapacity is fetched for the recommender only.
    assert metrics.requests[0] == [
        MetricRequest("Transactions", "Total"),
        MetricRequest("UsedCapacity", "Average"),
    ]
    assert metrics.granularities == [timedelta(hours=1)]
    assert summary.inventory_total == 6 and summary.evaluated == 2
    assert summary.skips["not_tierable"] == 3 and summary.skips["not_ready"] == 1
    assert [n for _, _, n, _ in metrics.calls] == [2]
    assert summary.findings == 1

    [fin] = rows(tmp_path, FINOPS_TABLE)
    assert fin["ResourceName"] == "st-cold" and fin["Sku"] == "Standard_GRS Hot"
    assert fin["MetricKey"] == "transactions" and fin["MetricName"] == "Transactions"
    assert fin["ObservedValue"] == 100.0 and fin["Percentile"] == 95 and fin["Threshold"] == 1000
    assert fin["Unit"] == "Count" and fin["Granularity"] == "PT1H"
    assert fin["RecommendedSku"] == "Standard_GRS Cool" and fin["Confidence"] == "low"
    assert "P95 hourly transactions 100 over 14d is below 1000." in fin["Reason"]
    assert "500 GiB in the Hot tier: set the default access tier to Cool" in fin["Reason"]
    assert fin["Reason"].endswith(UNPRICED_NOTE)
    assert fin["CurrentMonthlyCost"] is None and fin["ProjectedMonthlyCost"] is None
    assert fin["EstimatedMonthlySaving"] is None and fin["OsType"] == ""
    assert rows(tmp_path, OPS_TABLE) == []


async def test_finops_without_used_capacity_still_writes_an_unsized_row(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "st-cold", {"Transactions": 100.0})
    settings, clients, _ = make(tmp_path, config_dir)
    await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)
    [fin] = rows(tmp_path, FINOPS_TABLE)
    assert fin["RecommendedSku"] == "" and fin["Confidence"] == "low"
    assert "Used capacity unknown; cannot size." in fin["Reason"]


def test_batch_response_sums_the_filtered_series_and_keeps_value_less_points() -> None:
    """The recorded payload names the metrics exactly as thresholds config expects them."""
    prefix = (
        "/subscriptions/s1/resourceGroups/rg-app-prod/providers/Microsoft.Storage/storageAccounts/"
    )
    cold, hot = prefix + "st-cold", prefix + "st-hot"
    availability = MetricRequest("Availability", "Average")
    throttled = MetricRequest("Transactions", "Total", THROTTLE_FILTER, "ResponseType")
    ops = parse_batch_response(
        load_fixture("storage/metrics_batch.json"), [cold, hot], [availability, throttled]
    )
    assert [p.value for p in ops[cold][availability.name]] == [100.0, 100.0]
    assert [p.value for p in ops[hot][availability.name]] == [99.5]
    # Two ResponseType series come back for st-hot; the filtered request sums them per timestamp.
    assert [p.value for p in ops[hot][throttled.name]] == [4.0, 2.0]

    transactions = MetricRequest("Transactions", "Total")
    used = MetricRequest("UsedCapacity", "Average")
    fin = parse_batch_response(
        load_fixture("storage/metrics_batch.json"), [cold, hot], [transactions, used]
    )
    # An interval with nothing to count has a timeStamp and no value: None here, 0 after
    # missing_as_zero in metrics/derive.py.
    assert [p.value for p in fin[cold][transactions.name]] == [100.0, None]
    assert [p.value for p in fin[cold][used.name]] == [500.0 * GIB, 500.0 * GIB]
    assert fin[hot][used.name] == []
