from __future__ import annotations

from typing import Any, Protocol


class FindingsSink(Protocol):
    async def write(self, table: str, rows: list[dict[str, Any]]) -> str:
        """Write rows to a findings table; return where they went (for logs)."""
        ...
