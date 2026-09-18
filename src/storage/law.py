"""Thin Logs Ingestion client. The only module that imports azure.monitor.ingestion."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import HttpResponseError
from azure.monitor.ingestion.aio import LogsIngestionClient

from errors import PermissionMissing


class LogsIngestionSink:
    """Writes findings rows to Log Analytics through the findings DCR (stream Custom-<table>)."""

    def __init__(self, credential: AsyncTokenCredential, endpoint: str, rule_id: str) -> None:
        self._client = LogsIngestionClient(endpoint=endpoint, credential=credential)
        self._rule_id = rule_id

    async def write(self, table: str, rows: list[dict[str, Any]]) -> str:
        logs: list[MutableMapping[str, Any]] = list(rows)
        try:
            # The SDK splits into <=1 MB gzip chunks and raises on the first failed chunk.
            await self._client.upload(
                rule_id=self._rule_id, stream_name=f"Custom-{table}", logs=logs
            )
        except HttpResponseError as e:
            if e.status_code == 403:
                raise PermissionMissing("findings_ingest", f"DCR {self._rule_id}") from e
            raise
        return f"law:{table}"

    async def close(self) -> None:
        await self._client.close()
