from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from config.loader import load_config
from config.models import AppConfig
from evaluate.metric import evaluate_hot
from models import ColdFinding, HotAlert, MetricPoint, Recommendation, Resource
from notify.findings import (
    FINOPS_TABLE,
    OPS_TABLE,
    MetricContext,
    RunContext,
    finops_row,
    ops_row,
    ownership_columns,
)
from notify.sinks import LocalFindingsSink

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "datetime": (str,),
    "int": (int,),
    "real": (float, int),
    "dynamic": (list, dict),
}


def schema(repo_root: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((repo_root / "schema" / "findings-tables.json").read_text())
    return data["tables"]


def vm(tags: dict[str, str] | None = None) -> Resource:
    return Resource(
        kind="vm",
        id="/subscriptions/s1/resourceGroups/rg-app/providers/Microsoft.Compute/virtualMachines/vm1",
        name="vm1",
        type="microsoft.compute/virtualmachines",
        subscription_id="s1",
        resource_group="rg-app",
        location="eastus",
        sku="Standard_D8s_v5",
        tags=tags or {},
        props={"os_type": "Linux", "power_state": "PowerState/running"},
    )


@pytest.fixture
def cfg(config_dir: Path) -> AppConfig:
    return load_config(config_dir, "mg-prod")


def ctx(cfg: AppConfig) -> RunContext:
    return RunContext("run-1", "mg-prod", NOW, cfg.thresholds.tags, cfg.assignment_groups, "USD")


def mctx(cfg: AppConfig, aggregation: str = "average", granularity: str = "PT1H") -> MetricContext:
    vm_cfg = cfg.thresholds.resource_types["vm"]
    return MetricContext(
        vm_cfg.namespace,
        "cpu",
        vm_cfg.metrics["cpu"],
        aggregation,
        NOW - timedelta(hours=1),
        NOW,
        granularity,
    )


def sample_rows(cfg: AppConfig) -> dict[str, dict[str, Any]]:
    tags = {"owner": "a@example.com", "assignment_group": "cloud-engineering", "car_id": "7"}
    hot = HotAlert(vm(tags), "cpu", 97.5, 90, 60)
    cold = ColdFinding(vm(tags), "cpu", 6.1, 95, 20, 14, 0.98)
    rec = Recommendation(
        cold, "Standard_D4s_v5", "medium", "why", Decimal("560.64"), Decimal("280.32")
    )
    return {
        OPS_TABLE: ops_row(hot, ctx(cfg), mctx(cfg)),
        FINOPS_TABLE: finops_row(rec, ctx(cfg), mctx(cfg)),
    }


def test_rows_match_schema_columns_and_types(repo_root: Path, cfg: AppConfig) -> None:
    tables = schema(repo_root)
    rows = sample_rows(cfg)
    assert set(tables) == set(rows)
    for table, row in rows.items():
        columns = {c["name"]: c["type"] for c in tables[table]["columns"]}
        assert set(row) == set(columns), table
        for name, value in row.items():
            if value is not None:
                assert isinstance(value, PY_TYPES[columns[name]]), (table, name, value)


def test_schema_is_ingestible(repo_root: Path) -> None:
    for table, spec in schema(repo_root).items():
        assert table.endswith("_CL")
        names = [c["name"] for c in spec["columns"]]
        assert names[0] == "TimeGenerated" and len(names) == len(set(names))
        assert all(c["type"] in PY_TYPES and c["description"] for c in spec["columns"])


def test_ops_row_values(cfg: AppConfig) -> None:
    row = sample_rows(cfg)[OPS_TABLE]
    assert row["ResourceType"] == "microsoft.compute/virtualmachines"
    assert row["MetricName"] == "Percentage CPU" and row["Unit"] == "Percent"
    assert row["ObservedValue"] == 97.5 and row["Threshold"] == 90
    assert row["TimeGenerated"] == "2026-09-15T12:00:00Z"
    assert row["WindowStart"] == "2026-09-15T11:00:00Z"
    assert row["PortalUrl"].endswith("/virtualMachines/vm1/overview")
    assert row["AssignmentGroupEmail"] == "cloud-engineering@example.com"
    assert row["MissingTags"] == []


def test_finops_row_values(cfg: AppConfig) -> None:
    row = sample_rows(cfg)[FINOPS_TABLE]
    assert row["ResourceType"] == "microsoft.compute/virtualmachines"
    assert row["RecommendedSku"] == "Standard_D4s_v5" and row["Sku"] == "Standard_D8s_v5"
    assert row["EstimatedMonthlySaving"] == 280.32
    assert row["Percentile"] == 95 and row["Granularity"] == "PT1H"
    assert row["DataCoverage"] == 0.98 and row["OsType"] == "Linux"


def test_finops_row_without_pricing_or_target(cfg: AppConfig) -> None:
    rec = Recommendation(ColdFinding(vm(), "cpu", 6.1, 95, 20, 14, 1.0), None, "low", "unknown sku")
    row = finops_row(rec, ctx(cfg), mctx(cfg))
    assert row["RecommendedSku"] == ""
    assert row["CurrentMonthlyCost"] is None and row["EstimatedMonthlySaving"] is None


def test_finops_row_granularity_is_the_grain_the_series_was_fetched_at(cfg: AppConfig) -> None:
    """Not windows.finops.granularity: a type may override the FinOps grain (issue 2)."""
    rec = Recommendation(ColdFinding(vm(), "cpu", 6.1, 95, 20, 14, 1.0), None, "low", "why")
    assert cfg.thresholds.windows.finops.granularity == "PT1H"
    row = finops_row(rec, ctx(cfg), mctx(cfg, granularity="P1D"))
    assert row["Granularity"] == "P1D"


@pytest.mark.parametrize(
    ("tags", "missing"),
    [
        ({}, ["owner", "assignment_group", "car_id"]),
        (
            {"owner": "not-an-email", "assignment_group": "cloud-engineering", "car_id": "1"},
            ["owner"],
        ),
        (
            {"owner": "a@b.io", "assignment_group": "unmapped-team", "car_id": "1"},
            ["assignment_group"],
        ),
        ({"OWNER": "a@b.io", "Assignment_Group": "Cloud-Engineering", "car_id": "1"}, []),
    ],
)
def test_missing_tags(cfg: AppConfig, tags: dict[str, str], missing: list[str]) -> None:
    cols = ownership_columns(vm(tags), ctx(cfg))
    assert cols["MissingTags"] == missing


def test_unmapped_group_keeps_tag_value(cfg: AppConfig) -> None:
    cols = ownership_columns(vm({"assignment_group": "unmapped-team"}), ctx(cfg))
    assert cols["AssignmentGroup"] == "unmapped-team" and cols["AssignmentGroupEmail"] == ""


def test_threshold_source_tag_flows_to_row(cfg: AppConfig) -> None:
    th = cfg.thresholds
    points = [MetricPoint(NOW, 80.0)]
    cpu = th.resource_types["vm"].metrics["cpu"]
    hot, _ = evaluate_hot(vm({"o11y-threshold-cpu-hot": "75"}), points, "cpu", cpu, th)
    assert hot is not None
    row = ops_row(hot, ctx(cfg), mctx(cfg))
    assert row["Threshold"] == 75 and row["ThresholdSource"] == "tag"


async def test_local_sink_appends_jsonl(tmp_path: Path) -> None:
    sink = LocalFindingsSink(tmp_path)
    await sink.write(OPS_TABLE, [{"a": 1}])
    where = await sink.write(OPS_TABLE, [{"a": 2}])
    lines = Path(where).read_text().splitlines()
    assert [json.loads(line)["a"] for line in lines] == [1, 2]
