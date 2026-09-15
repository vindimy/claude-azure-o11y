from __future__ import annotations

from typing import Protocol

from models import HotAlert


class Notifier(Protocol):
    async def send_hot(self, alert: HotAlert) -> None: ...


class ReportSink(Protocol):
    async def write(self, relative_path: str, markdown: str) -> str: ...
