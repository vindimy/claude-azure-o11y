"""Ops alert de-duplication. In-memory cache persisted through a SuppressionStore."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol


class SuppressionStore(Protocol):
    async def load(self) -> dict[str, str]: ...

    async def save(self, data: dict[str, str]) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SuppressionCache:
    def __init__(
        self,
        window: timedelta,
        entries: dict[str, datetime] | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._window = window
        self._entries = dict(entries or {})
        self._now = now

    @staticmethod
    def key(resource_id: str, metric: str) -> str:
        return f"{resource_id.lower()}|{metric}"

    def should_send(self, key: str) -> bool:
        last = self._entries.get(key)
        return last is None or (self._now() - last) > self._window

    def mark_sent(self, key: str) -> None:
        self._entries[key] = self._now()

    def to_dict(self) -> dict[str, str]:
        now = self._now()
        return {k: v.isoformat() for k, v in self._entries.items() if (now - v) <= self._window}

    @classmethod
    async def load(
        cls,
        store: SuppressionStore,
        window: timedelta,
        now: Callable[[], datetime] | None = None,
    ) -> SuppressionCache:
        raw = await store.load()
        entries: dict[str, datetime] = {}
        for k, v in raw.items():
            try:
                entries[k] = datetime.fromisoformat(v)
            except (TypeError, ValueError):
                continue
        return cls(window, entries, now or _utcnow)

    async def save(self, store: SuppressionStore) -> None:
        await store.save(self.to_dict())


class LocalSuppressionStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    async def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        data = json.loads(self._path.read_text() or "{}")
        return {str(k): str(v) for k, v in data.items()}

    async def save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True))
