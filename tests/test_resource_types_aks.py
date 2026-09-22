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
from resource_types.aks import KIND, active, parse, parse_pool
from tests.conftest import load_fixture
from tests.test_pipeline import FAKE_VALUES, FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)


def clusters() -> list[Resource]:
    return [parse(row) for row in load_fixture("aks/resource_graph.json")["data"]]


def cluster(name: str) -> Resource:
    return next(c for c in clusters() if c.name == name)


class FakeInventory:
    """Serves the recorded AKS fixture; every other kind is empty."""

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        return clusters() if kind == KIND else []


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


def test_parse_reads_the_pools_into_sku_and_props() -> None:
    hot = cluster("aks-hot")
    assert hot.type == "microsoft.containerservice/managedclusters"
    assert hot.sku == "Standard_D4s_v5 x3 + Standard_D8s_v5 x2"
    assert hot.prop("tier") == "Standard" and hot.prop("power_state") == "Running"
    assert hot.prop("k8s_version") == "1.30.4" and hot.prop("node_count") == 5
    assert hot.prop("pools") == [
        {
            "name": "system",
            "vm_size": "Standard_D4s_v5",
            "count": 3,
            "mode": "System",
            "autoscale": False,
            "min_count": 0,
            "max_count": 0,
        },
        {
            "name": "userpool",
            "vm_size": "Standard_D8s_v5",
            "count": 2,
            "mode": "User",
            "autoscale": True,
            "min_count": 2,
            "max_count": 5,
        },
    ]
    assert hot.subscription_id == "s1" and hot.resource_group == "rg-app-prod"
    assert hot.tags == {
        "car_id": "200",
        "owner": "alice@example.com",
        "assignment_group": "cloud-engineering",
    }


def test_parse_handles_a_cluster_without_pools() -> None:
    nopools = cluster("aks-nopools")
    assert nopools.sku == "" and nopools.prop("pools") == [] and nopools.prop("node_count") == 0


def test_parse_pool_defaults_missing_profile_fields() -> None:
    assert parse_pool({"name": "spot"}) == {
        "name": "spot",
        "vm_size": "",
        "count": 0,
        "mode": "",
        "autoscale": False,
        "min_count": 0,
        "max_count": 0,
    }
    assert parse_pool({}) == parse_pool({"name": None, "count": None, "enableAutoScaling": None})
    row = {
        "id": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.ContainerService"
        "/managedClusters/aks-bare",
        "name": "aks-bare",
        "subscriptionId": "s1",
        "resourceGroup": "rg",
        "location": "eastus",
        "tags": None,
        "pools": [{"vmSize": "Standard_D2s_v5", "count": 1}, "not-a-profile"],
    }
    bare = parse(row)
    assert bare.sku == "Standard_D2s_v5 x1" and bare.prop("node_count") == 1
    assert bare.prop("tier") == "" and bare.prop("power_state") == ""
    assert bare.prop("k8s_version") == "" and bare.tags == {}


def test_only_running_clusters_are_active() -> None:
    skip = active(cluster("aks-stopped"))
    assert skip is not None
    assert skip.reason == "not_running" and skip.detail == "Stopped"
    assert active(cluster("aks-hot")) is None
    assert active(cluster("aks-nopools")) is None


async def test_ops_run_writes_hot_rows_and_skips_stopped_clusters(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "aks-hot",
        {
            "node_cpu_usage_percentage": 95.0,
            "node_memory_working_set_percentage": 92.0,
            "node_disk_usage_percentage": 50.0,
            "cluster_autoscaler_unschedulable_pods_count": 2.0,
        },
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "ops", now=NOW)

    assert [m.name for m in metrics.requests[0]] == [
        "node_cpu_usage_percentage",
        "node_memory_working_set_percentage",
        "node_disk_usage_percentage",
        "cluster_autoscaler_unschedulable_pods_count",
    ]
    assert metrics.granularities == [timedelta(minutes=1)]
    assert summary.inventory_total == 4 and summary.evaluated == 3
    assert summary.skips["not_running"] == 1
    ops = rows(tmp_path, OPS_TABLE)
    assert sorted((r["ResourceName"], r["MetricKey"]) for r in ops) == [
        ("aks-hot", "node_cpu"),
        ("aks-hot", "node_memory"),
        ("aks-hot", "unschedulable_pods"),
    ]
    cpu = next(r for r in ops if r["MetricKey"] == "node_cpu")
    assert cpu["MetricName"] == "node_cpu_usage_percentage" and cpu["Aggregation"] == "Average"
    assert cpu["ObservedValue"] == 95.0 and cpu["Threshold"] == 90
    assert cpu["MetricNamespace"] == "Microsoft.ContainerService/managedClusters"
    assert cpu["ResourceType"] == "microsoft.containerservice/managedclusters"
    assert cpu["Sku"] == "Standard_D4s_v5 x3 + Standard_D8s_v5 x2" and cpu["Location"] == "eastus"
    assert cpu["Owner"] == "alice@example.com" and cpu["AssignmentGroup"] == "cloud-engineering"
    pods = next(r for r in ops if r["MetricKey"] == "unschedulable_pods")
    assert pods["MetricName"] == "cluster_autoscaler_unschedulable_pods_count"
    assert pods["ObservedValue"] == 2.0 and pods["Threshold"] == 1
    assert rows(tmp_path, FINOPS_TABLE) == []


async def test_finops_run_recommends_fewer_nodes_per_pool(
    tmp_path: Path, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        FAKE_VALUES,
        "aks-cold",
        {
            "node_cpu_usage_percentage": 18.0,
            "node_memory_working_set_percentage": 20.0,
            "cluster_autoscaler_unneeded_nodes_count": 1.0,
        },
    )
    settings, clients, metrics = make(tmp_path, config_dir)
    summary = await run(settings, load_config(config_dir, "mg-prod"), clients, "finops", now=NOW)

    # The autoscaler count carries no threshold: it is fetched for the recommender only.
    assert [m.name for m in metrics.requests[0]] == [
        "node_cpu_usage_percentage",
        "node_memory_working_set_percentage",
        "cluster_autoscaler_unneeded_nodes_count",
    ]
    assert metrics.granularities == [timedelta(hours=1)]
    assert summary.inventory_total == 4 and summary.skips["not_running"] == 1
    assert summary.findings == 1

    [fin] = rows(tmp_path, FINOPS_TABLE)
    assert fin["ResourceName"] == "aks-cold"
    assert fin["Sku"] == "Standard_D4s_v5 x3 + Standard_D8s_v5 x2"
    assert fin["MetricKey"] == "node_cpu" and fin["ObservedValue"] == 18.0
    assert fin["Percentile"] == 95 and fin["Threshold"] == 20
    # Cluster peak is the memory P95 (20%): 3 x 0.2 x 1.3 -> 1 node; autoscale min 2 -> 1.
    assert fin["RecommendedSku"] == "system: Standard_D4s_v5 x1; userpool: min 1"
    assert fin["Confidence"] == "low"
    assert "P95 node CPU 18% over 14d is below 20%." in fin["Reason"]
    assert "Autoscaler reports 1 unneeded nodes." in fin["Reason"]
    assert fin["Reason"].endswith("verify per node pool. " + UNPRICED_NOTE)
    assert fin["EstimatedMonthlySaving"] is None and fin["OsType"] == ""
    assert fin["CurrentMonthlyCost"] is None and fin["ProjectedMonthlyCost"] is None
    assert fin["Granularity"] == "PT1H"
    assert rows(tmp_path, OPS_TABLE) == []


def test_batch_response_maps_aks_metrics_by_their_aggregation() -> None:
    """The recorded batch payload names the metrics exactly as thresholds config expects them."""
    prefix = (
        "/subscriptions/s1/resourceGroups/rg-app-prod/providers/Microsoft.ContainerService/"
        "managedClusters/"
    )
    cold, hot = prefix + "aks-cold", prefix + "aks-hot"
    cpu = MetricRequest("node_cpu_usage_percentage", "Average")
    memory = MetricRequest("node_memory_working_set_percentage", "Average")
    disk = MetricRequest("node_disk_usage_percentage", "Average")
    unschedulable = MetricRequest("cluster_autoscaler_unschedulable_pods_count", "Average")
    unneeded = MetricRequest("cluster_autoscaler_unneeded_nodes_count", "Average")
    requests = [cpu, memory, disk, unschedulable, unneeded]
    out = parse_batch_response(load_fixture("aks/metrics_batch.json"), [cold, hot], requests)
    assert [p.value for p in out[cold][cpu.name]] == [18.0, 22.0, None]
    assert [p.value for p in out[cold][memory.name]] == [20.0, 19.0]
    assert [p.value for p in out[cold][unneeded.name]] == [1.0, 1.0]
    assert out[cold][disk.name] == []
    assert [p.value for p in out[hot][cpu.name]] == [95.0, 97.0]
    assert [p.value for p in out[hot][memory.name]] == [92.0]
    assert [p.value for p in out[hot][disk.name]] == [50.0]
    assert [p.value for p in out[hot][unschedulable.name]] == [2.0]
    assert out[hot][unneeded.name] == []
