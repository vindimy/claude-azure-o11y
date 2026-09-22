from __future__ import annotations

from typing import Any

import pytest
from azure.core.exceptions import HttpResponseError
from azure.mgmt.resourcegraph.models import QueryRequest

from errors import PermissionMissing
from inventory.graph import ResourceGraphInventory
from models import Scope
from resource_types import TYPES
from resource_types.vm import QUERY, parse
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
    vm = parse(row)
    assert vm.kind == "vm" and vm.type == "microsoft.compute/virtualmachines"
    assert vm.tags == {} and vm.sku == "Standard_B1s" and vm.resource_group == "RG"
    assert vm.prop("os_type") == "" and vm.prop("power_state") == ""


async def test_paginates_and_scopes_to_management_group() -> None:
    fake = FakeRgClient()
    vms = await _inventory(fake).list_resources("vm", Scope("management_group", ["mg-prod"]))
    names = [v.name for v in vms]
    assert names == ["vm-hot", "vm-cold", "vm-nodata", "vm-dev", "vm-stopped", "vm-excluded"]
    assert len(fake.requests) == 2
    first, second = fake.requests
    assert first.management_groups == ["mg-prod"] and first.subscriptions is None
    assert second.options is not None and second.options.skip_token == "TOKEN-2"
    assert first.query.strip() == QUERY.strip()


async def test_subscription_scope() -> None:
    fake = FakeRgClient()
    await _inventory(fake).list_resources("vm", Scope("subscriptions", ["s1", "s2"]))
    assert fake.requests[0].subscriptions == ["s1", "s2"]
    assert fake.requests[0].management_groups is None


async def test_unknown_kind_is_a_programming_error() -> None:
    with pytest.raises(KeyError):
        await _inventory(FakeRgClient()).list_resources("nope", Scope("subscriptions", ["s1"]))


async def test_403_becomes_permission_missing() -> None:
    class Denied(FakeRgClient):
        async def resources(self, query: QueryRequest) -> FakeResponse:
            err = HttpResponseError(message="denied")
            err.status_code = 403
            raise err

    with pytest.raises(PermissionMissing) as info:
        await _inventory(Denied()).list_resources("vm", Scope("subscriptions", ["s1"]))
    assert info.value.need == "inventory"


def test_every_registered_type_has_a_query_and_parser() -> None:
    for kind, spec in TYPES.items():
        assert spec.kind == kind and "resources" in spec.query and spec.arm_type.islower()
        assert callable(spec.parse) and callable(spec.active)
