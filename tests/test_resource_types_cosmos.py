from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from config.loader import load_config
from config.settings import Settings
from models import Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import Clients, run
from resource_types.cosmos import KIND, finops_skip, parse
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def accounts() -> list[Resource]:
    return [parse(row) for row in load_fixture("cosmos/resource_graph.json")["data"]]


def account(name: str) -> Resource:
    return next(a for a in accounts() if a.name == name)


class FakeInventory:
    """Serves the recorded Cosmos DB fixture; every other kind is empty."""

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


def test_parse_reads_the_capacity_mode_into_sku_and_props() -> None:
    cold = account("cosmos-cold")
    assert cold.type == "microsoft.documentdb/databaseaccounts"
    assert cold.sku == "provisioned" and cold.prop("capacity_mode") == "provisioned"
    assert cold.prop("api_kind") == "MongoDB" and cold.prop("enable_free_tier") is True
    assert cold.subscription_id == "s1" and cold.resource_group == "rg-app-prod"
    assert cold.tags == {"car_id": "200"}


def test_parse_marks_serverless_accounts() -> None:
    serverless = account("cosmos-serverless")
    assert serverless.sku == "serverless" and serverless.prop("capacity_mode") == "serverless"
    assert serverless.prop("enable_free_tier") is False


def test_serverless_accounts_are_skipped_in_finops_only() -> None:
    skip = finops_skip(account("cosmos-serverless"))
    assert skip is not None
    assert skip.reason == "no_capacity_model" and skip.detail == "serverless"
    assert finops_skip(account("cosmos-cold")) is None


async def test_ops_run_uses_the_pt5m_grain_and_writes_hot_rows(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "cosmos-hot",
        {"NormalizedRUConsumption": 96.0, "ThrottledRequestPercentage": 7.0},
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "ops", now=NOW)

    assert metrics.granularities == [timedelta(minutes=5)]
    assert summary.inventory_total == 3 and summary.evaluated == 3
    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [
        ("cosmos-hot", "ru"),
        ("cosmos-hot", "throttled"),
    ]
    ru = next(r for r in ops if r["MetricKey"] == "ru")
    assert ru["MetricName"] == "NormalizedRUConsumption" and ru["Aggregation"] == "Maximum"
    assert ru["ObservedValue"] == 96.0 and ru["Threshold"] == 90
    assert ru["MetricNamespace"] == "Microsoft.DocumentDB/databaseAccounts"
    assert ru["ResourceType"] == "microsoft.documentdb/databaseaccounts"
    assert ru["Sku"] == "provisioned" and ru["Location"] == "eastus"
    throttled = next(r for r in ops if r["MetricKey"] == "throttled")
    assert throttled["MetricName"] == "ThrottledRequestPercentage"
    assert throttled["ObservedValue"] == 7.0 and throttled["Threshold"] == 5
    assert rows(tmp_path, FINOPS_TABLE) == []


async def test_finops_run_recommends_lower_throughput_and_skips_serverless(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "cosmos-cold",
        {"NormalizedRUConsumption": 10.0, "ProvisionedThroughput": 4000.0},
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)

    # Throughput metrics carry no threshold: they are fetched for the recommender only.
    assert [m.name for m in metrics.requests[0]] == [
        "NormalizedRUConsumption",
        "ProvisionedThroughput",
        "AutoscaleMaxThroughput",
    ]
    assert metrics.granularities == [timedelta(hours=1)]
    assert summary.skips["no_capacity_model"] == 1
    assert summary.findings == 1

    [fin] = rows(tmp_path, FINOPS_TABLE)
    assert fin["ResourceName"] == "cosmos-cold" and fin["Sku"] == "provisioned"
    assert fin["MetricKey"] == "ru" and fin["ObservedValue"] == 10.0 and fin["Percentile"] == 95
    assert fin["RecommendedSku"] == "600 RU/s" and fin["Confidence"] == "low"
    assert "Lower provisioned throughput from 4000 RU/s." in fin["Reason"]
    assert fin["EstimatedMonthlySaving"] is None and fin["OsType"] == ""
    assert rows(tmp_path, OPS_TABLE) == []
