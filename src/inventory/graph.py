"""Resource Graph inventory for every registered type.

The only module importing azure.mgmt.resourcegraph. Queries and parsers live in resource_types/.
"""

from __future__ import annotations

from typing import Any

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError
from azure.mgmt.resourcegraph.aio import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions

from errors import PermissionMissing
from models import Resource, Scope
from resource_types import TYPES

PAGE_SIZE = 1000


class ResourceGraphInventory:
    def __init__(self, credential: AsyncTokenCredential) -> None:
        self._client = ResourceGraphClient(credential)

    async def list_resources(self, kind: str, scope: Scope) -> list[Resource]:
        spec = TYPES[kind]
        out: list[Resource] = []
        skip_token: str | None = None
        while True:
            request = QueryRequest(
                query=spec.query,
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
            out.extend(spec.parse(row) for row in rows)
            skip_token = response.skip_token
            if not skip_token:
                return out

    async def close(self) -> None:
        await self._client.close()
