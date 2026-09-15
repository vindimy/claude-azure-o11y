from __future__ import annotations

from typing import Any

from azure.mgmt.resourcegraph.models import QueryRequest

from inventory.vms import VM_QUERY, ResourceGraphInventory, parse_vm_row
from models import Scope
from tests.conftest import load_fixture


class FakeResponse:
    def __init__(self, page: dict[str, Any]) -> None:
        self.data = page["data"]
        self.skip_token = page["skipToken"]


class FakeRgClient:
    def __init__(self) -> None:
        self.requests: list[QueryRequest] = []

    async def resources(self, query: QueryRequest) -> FakeResponse:
        self.requests.append(query)
        paged = query.options is not None and query.options.skip_token
        page = "resource_graph_vms_page2.json" if paged else "resource_graph_vms_page1.json"
        return FakeResponse(load_fixture(page))

    async def close(self) -> None:
        pass


def _inventory(fake: FakeRgClient) -> ResourceGraphInventory:
    inv = ResourceGraphInventory.__new__(ResourceGraphInventory)
    inv._client = fake  # type: ignore[assignment]
    return inv


def test_parse_vm_row_handles_missing_fields() -> None:
    row = {
        "id": "/x",
        "name": "n",
        "subscriptionId": "s",
        "resourceGroup": "RG",
        "location": "eastus",
        "tags": None,
        "vmSize": "Standard_B1s",
        "osType": None,
        "powerState": None,
    }
    vm = parse_vm_row(row)
    assert vm.tags == {} and vm.os_type == "" and vm.power_state == "" and vm.resource_group == "RG"


async def test_paginates_and_scopes_to_management_group() -> None:
    fake = FakeRgClient()
    vms = await _inventory(fake).list_vms(Scope("management_group", ["mg-prod"]))
    names = [v.name for v in vms]
    assert names == ["vm-hot", "vm-cold", "vm-nodata", "vm-dev", "vm-stopped", "vm-excluded"]
    assert len(fake.requests) == 2
    first, second = fake.requests
    assert first.management_groups == ["mg-prod"] and first.subscriptions is None
    assert second.options is not None and second.options.skip_token == "TOKEN-2"
    assert first.query.strip() == VM_QUERY.strip()


async def test_subscription_scope() -> None:
    fake = FakeRgClient()
    await _inventory(fake).list_vms(Scope("subscriptions", ["s1", "s2"]))
    assert fake.requests[0].subscriptions == ["s1", "s2"]
    assert fake.requests[0].management_groups is None
