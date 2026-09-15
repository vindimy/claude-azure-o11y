"""Resource Graph inventory for VMs. The only module importing azure.mgmt.resourcegraph."""

from __future__ import annotations

from typing import Any

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError
from azure.mgmt.resourcegraph.aio import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

from errors import PermissionMissing
from models import Scope, VmResource

VM_QUERY = """
resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, subscriptionId, resourceGroup, location, tags,
          vmSize = tostring(properties.hardwareProfile.vmSize),
          osType = tostring(properties.storageProfile.osDisk.osType),
          powerState = tostring(properties.extended.instanceView.powerState.code)
| order by id asc
"""

PAGE_SIZE = 1000


def parse_vm_row(row: dict[str, Any]) -> VmResource:
    tags = row.get("tags") or {}
    return VmResource(
        id=str(row["id"]),
        name=str(row["name"]),
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        vm_size=str(row.get("vmSize") or ""),
        os_type=str(row.get("osType") or ""),
        power_state=str(row.get("powerState") or ""),
        tags={str(k): str(v) for k, v in tags.items() if v is not None},
    )


class ResourceGraphInventory:
    def __init__(self, credential: AsyncTokenCredential) -> None:
        self._client = ResourceGraphClient(credential)

    async def list_vms(self, scope: Scope) -> list[VmResource]:
        out: list[VmResource] = []
        skip_token: str | None = None
        while True:
            request = QueryRequest(
                query=VM_QUERY,
                management_groups=scope.values if scope.kind == "management_group" else None,
                subscriptions=scope.values if scope.kind == "subscriptions" else None,
                options=QueryRequestOptions(top=PAGE_SIZE, skip_token=skip_token),
            )
            try:
                response = await self._client.resources(request)
            except HttpResponseError as e:
                if e.status_code == 403:
                    raise PermissionMissing("inventory", str(scope.values)) from e
                raise
            rows: Any = response.data or []
            out.extend(parse_vm_row(row) for row in rows)
            skip_token = response.skip_token
            if not skip_token:
                return out

    async def close(self) -> None:
        await self._client.close()
