"""Thin blob client. The only module that imports azure.storage.blob."""

from __future__ import annotations

import json

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob import ContentSettings
from azure.storage.blob.aio import BlobServiceClient

from errors import PermissionMissing


class BlobStore:
    def __init__(self, credential: AsyncTokenCredential, account_name: str) -> None:
        self._svc = BlobServiceClient(
            account_url=f"https://{account_name}.blob.core.windows.net", credential=credential
        )

    async def read_text(self, container: str, name: str) -> str | None:
        blob = self._svc.get_blob_client(container, name)
        try:
            downloader = await blob.download_blob()
            data = await downloader.readall()
        except ResourceNotFoundError:
            return None
        except HttpResponseError as e:
            if e.status_code == 403:
                raise PermissionMissing(container) from e
            raise
        return bytes(data).decode("utf-8")

    async def write_text(
        self, container: str, name: str, text: str, content_type: str = "text/plain"
    ) -> str:
        blob = self._svc.get_blob_client(container, name)
        try:
            await blob.upload_blob(
                text.encode("utf-8"),
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        except HttpResponseError as e:
            if e.status_code == 403:
                raise PermissionMissing(container) from e
            raise
        return str(blob.url)

    async def close(self) -> None:
        await self._svc.close()


class BlobSuppressionStore:
    def __init__(self, store: BlobStore, container: str, name: str) -> None:
        self._store, self._container, self._name = store, container, name

    async def load(self) -> dict[str, str]:
        text = await self._store.read_text(self._container, self._name)
        if not text:
            return {}
        data = json.loads(text)
        return {str(k): str(v) for k, v in data.items()}

    async def save(self, data: dict[str, str]) -> None:
        await self._store.write_text(
            self._container,
            self._name,
            json.dumps(data, indent=2, sort_keys=True),
            "application/json",
        )
