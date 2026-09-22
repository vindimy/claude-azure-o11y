"""Virtual network subnets: usable-IP math, parsing, and an end-to-end Ops/FinOps run.

Own FakeInventory (per context-core.md); tests/test_pipeline.py is not touched. FakeMetrics,
FakePricing, and `rows` are reused by import since vnet has no Azure Monitor metrics of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from config.loader import load_config
from config.settings import Settings
from models import Resource, Scope
from notify.findings import FINOPS_TABLE, OPS_TABLE
from notify.sinks import LocalFindingsSink
from pipeline import Clients, run
from resource_types.vnet import parse, usable_ips
from tests.conftest import load_fixture
from tests.test_pipeline import FakeMetrics, FakePricing, rows

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def test_usable_ips_single_prefix() -> None:
    assert usable_ips(["10.0.0.0/24"]) == 251


def test_usable_ips_multiple_prefixes_add_up() -> None:
    assert usable_ips(["10.0.0.0/24", "10.0.1.0/28"]) == 251 + 11


def test_usable_ips_ignores_ipv6_prefixes() -> None:
    assert usable_ips(["10.0.0.0/24", "2001:db8::/64"]) == 251


def test_usable_ips_skips_a_malformed_prefix_instead_of_failing_the_type() -> None:
    """One bad Resource Graph row must not count the whole vnet type as a type_failure."""
    assert usable_ips(["10.0.0.0", "10.0.1.0/28"]) == 11
    assert usable_ips(["10.0.0.0/nope", "10.0.1.0/28"]) == 11
    assert usable_ips([""]) == 0


def _fixture_rows() -> list[dict[str, object]]:
    data = load_fixture("vnet/resource_graph.json")["data"]
    assert isinstance(data, list)
    return data


def test_parse_single_prefix_subnet_utilization() -> None:
    row = next(r for r in _fixture_rows() if str(r["name"]).endswith("/subnet-a"))
    resource = parse(row)
    assert resource.kind == "vnet"
    assert resource.type == "microsoft.network/virtualnetworks/subnets"
    assert resource.name == "vnet-app/subnet-a"
    assert resource.sku == "10.0.1.0/27"
    assert resource.prop("prefixes") == ["10.0.1.0/27"]
    assert resource.prop("ip_usable") == 27
    assert resource.prop("ip_used") == 26
    assert resource.prop("utilization_percent") == 96.3
    assert resource.prop("delegated") is False
    assert resource.prop("vnet_id") == (
        "/subscriptions/s1/resourceGroups/rg-net-prod/providers/"
        "Microsoft.Network/virtualNetworks/vnet-app"
    )


def test_parse_multi_prefix_subnet_is_cold() -> None:
    row = next(r for r in _fixture_rows() if str(r["name"]).endswith("/subnet-b"))
    resource = parse(row)
    assert resource.prop("prefixes") == ["10.0.2.0/24", "10.0.3.0/25"]
    assert resource.prop("ip_usable") == 251 + 123
    assert resource.prop("ip_used") == 10
    assert resource.prop("delegated") is True
    assert resource.prop("utilization_percent") < 80


def test_parse_zero_usable_ips_is_zero_percent() -> None:
    row = {
        "id": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Network/"
        "virtualNetworks/vnet-x/subnets/empty",
        "vnetId": "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Network/"
        "virtualNetworks/vnet-x",
        "name": "vnet-x/empty",
        "subscriptionId": "s1",
        "resourceGroup": "rg",
        "location": "eastus",
        "tags": {},
        "prefix": None,
        "prefixes": None,
        "ipUsed": 0,
        "delegated": False,
    }
    resource = parse(row)
    assert resource.prop("ip_usable") == 0
    assert resource.prop("utilization_percent") == 0.0


class FakeInventory:
    """Serves the vnet fixture only; every other kind is empty."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        self.kinds.append(kind)
        if kind == "vnet":
            return [parse(r) for r in _fixture_rows()]
        return []


def make(tmp_path: Path, config_dir: Path) -> tuple[Settings, Clients, FakeMetrics]:
    settings = Settings(
        mg_id="mg-prod",
        dry_run=True,
        output_dir=tmp_path,
        config_dir=config_dir,
        resource_types="vnet",
    )
    metrics = FakeMetrics()
    clients = Clients(
        inventory=FakeInventory(),
        metrics=metrics,
        pricing=FakePricing(),
        findings=LocalFindingsSink(tmp_path),
    )
    return settings, clients, metrics


async def test_ops_run_writes_one_row_for_the_hot_subnet(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "ops", now=NOW, run_id="run-vnet")

    assert summary.mode == "ops"
    ops = rows(tmp_path, OPS_TABLE)
    assert len(ops) == 1
    [row] = ops
    assert row["ResourceId"].endswith("/subnets/subnet-a")
    assert row["ResourceName"] == "vnet-app/subnet-a"
    assert row["ObservedValue"] == 96.3
    assert row["Aggregation"] == "computed"
    assert row["MetricNamespace"] == "Microsoft.Network/virtualNetworks"
    assert row["MetricName"] == "SubnetIpUtilization"
    assert row["MetricKey"] == "subnet_ip"
    assert rows(tmp_path, FINOPS_TABLE) == []
    assert metrics.calls == []


async def test_finops_run_writes_no_rows(tmp_path: Path, config_dir: Path) -> None:
    settings, clients, metrics = make(tmp_path, config_dir)
    cfg = load_config(config_dir, "mg-prod")
    summary = await run(settings, cfg, clients, "finops", now=NOW)

    assert summary.findings == 0
    assert rows(tmp_path, FINOPS_TABLE) == []
    assert rows(tmp_path, OPS_TABLE) == []
    assert metrics.calls == []
