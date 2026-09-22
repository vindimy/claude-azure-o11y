from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.loader import load_config
from config.settings import Settings
from metrics.batch import parse_batch_response
from models import MetricRequest, Resource, Scope, Skip
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import Clients, run
from resource_types.sqldb import ARM_TYPE, QUERY, parse, purchasing_model
from resource_types.sqldb import SPEC as SQLDB
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, NOW, FakeMetrics, FakePricing, rows

SERVER = "/subscriptions/s1/resourceGroups/rg-app-prod/providers/Microsoft.Sql/servers/sql-prod"
PREFIX = f"{SERVER}/databases/"
POOL = f"{SERVER}/elasticPools/pool-shared"


def graph_rows() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = load_fixture("sqldb/resource_graph.json")["data"]
    return data


def served_rows() -> list[dict[str, Any]]:
    """What Resource Graph returns once QUERY's own filters ran (no master, no data warehouse)."""
    return [
        r
        for r in graph_rows()
        if r["name"].lower() != "master" and "datawarehouse" not in str(r["kind"]).lower()
    ]


def by_name(name: str) -> Resource:
    return parse(next(r for r in graph_rows() if r["name"] == name))


class FakeInventory:
    """Serves the sqldb fixture rows; every other kind is empty."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        if kind == SQLDB.kind:
            return [parse(r) for r in served_rows()]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types="sqldb",
    )
    clients = Clients(
        inventory=FakeInventory(),
        metrics=FakeMetrics(),
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients


def test_query_excludes_master_and_data_warehouses() -> None:
    assert "name !~ 'master'" in QUERY
    assert "datawarehouse" in QUERY
    assert served_rows() == [r for r in graph_rows() if r["name"] != "master"]


@pytest.mark.parametrize(
    ("name", "model"),
    [("db-cold", "dtu"), ("db-hot", "vcore"), ("db-paused", "serverless")],
)
def test_parse_maps_the_purchasing_model(name: str, model: str) -> None:
    assert by_name(name).prop("purchasing_model") == model


def test_parse_maps_every_prop() -> None:
    db = by_name("db-hot")
    assert db.kind == "sqldb" and db.type == ARM_TYPE
    assert db.name == "db-hot" and db.id == PREFIX + "db-hot"
    assert db.subscription_id == "s1" and db.resource_group == "rg-app-prod"
    assert db.location == "eastus" and db.tag("car_id") == "100"
    assert db.sku == "GP_Gen5_8" and db.prop("sku_name") == "GP_Gen5_8"
    assert db.prop("tier") == "GeneralPurpose" and db.prop("capacity") == 8
    assert db.prop("status") == "Online" and db.prop("db_kind") == "v12.0,user,pool"
    assert db.prop("pool_id") == POOL
    assert db.prop("hyperscale") is False


def test_purchasing_model_of_every_shape() -> None:
    assert purchasing_model("Standard", "S3") == "dtu"
    assert purchasing_model("Basic", "Basic") == "dtu"
    assert purchasing_model("Premium", "P2") == "dtu"
    assert purchasing_model("GeneralPurpose", "GP_Gen5_8") == "vcore"
    assert purchasing_model("GeneralPurpose", "GP_S_Gen5_4") == "serverless"
    assert purchasing_model("Hyperscale", "HS_Gen5_4") == "vcore"


def test_parse_flags_hyperscale() -> None:
    row = dict(graph_rows()[1], tier="Hyperscale", skuName="HS_Gen5_4")
    assert parse(row).prop("hyperscale") is True


def test_active_skips_a_paused_database() -> None:
    skip = SQLDB.active(by_name("db-paused"))
    assert skip == Skip(PREFIX + "db-paused", "not_online", "Paused")
    assert SQLDB.active(by_name("db-cold")) is None


def test_finops_skips_a_database_in_an_elastic_pool() -> None:
    pooled = by_name("db-hot")
    skip = SQLDB.finops_skip(pooled)
    assert skip == Skip(pooled.id, "in_elastic_pool", str(pooled.prop("pool_id")))
    assert SQLDB.finops_skip(by_name("db-cold")) is None


def test_batch_response_maps_sql_metrics() -> None:
    hot, cold = PREFIX + "db-hot", PREFIX + "db-cold"
    dtu = MetricRequest("dtu_consumption_percent", "Average")
    cpu = MetricRequest("cpu_percent", "Average")
    out = parse_batch_response(load_fixture("sqldb/metrics_batch.json"), [hot, cold], [cpu, dtu])
    assert [p.value for p in out[hot][cpu.name]] == [95.0, 97.5, None]
    assert [p.value for p in out[cold][dtu.name]] == [5.0]
    assert out[cold][cpu.name] == []


async def test_ops_run_flags_the_hot_database(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "db-hot", {"cpu_percent": 95.0})
    monkeypatch.setitem(FAKE_VALUES, "db-cold", {"dtu_consumption_percent": 5.0})
    settings, clients = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "ops", now=NOW)

    assert summary.inventory_total == 3 and summary.evaluated == 2
    assert summary.skips["not_online"] == 1
    assert [(r["ResourceName"], r["MetricKey"]) for r in rows(tmp_path, OPS_TABLE)] == [
        ("db-hot", "cpu")
    ]
    [hot] = rows(tmp_path, OPS_TABLE)
    assert hot["MetricName"] == "cpu_percent" and hot["ObservedValue"] == 95.0
    assert hot["ResourceType"] == ARM_TYPE and hot["Sku"] == "GP_Gen5_8"
    assert rows(tmp_path, FINOPS_TABLE) == []


async def test_finops_run_recommends_a_smaller_objective(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(FAKE_VALUES, "db-hot", {"cpu_percent": 95.0})
    monkeypatch.setitem(FAKE_VALUES, "db-cold", {"dtu_consumption_percent": 5.0})
    settings, clients = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)

    assert summary.skips["in_elastic_pool"] == 1  # db-hot is pooled: Ops only
    assert summary.findings == 1
    [cold] = rows(tmp_path, FINOPS_TABLE)
    assert cold["ResourceName"] == "db-cold" and cold["Sku"] == "S3"
    assert cold["MetricKey"] == "dtu" and cold["ObservedValue"] == 5.0
    assert cold["RecommendedSku"] == "S0" and cold["Confidence"] == "medium"
    assert cold["Reason"].endswith("Pricing not implemented for SQL.")
    assert cold["CurrentMonthlyCost"] is None and cold["EstimatedMonthlySaving"] is None
