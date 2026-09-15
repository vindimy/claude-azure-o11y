"""Protocols the pipeline depends on.

Real implementations live in inventory/, metrics/ and recommend/pricing.py.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from metrics.batch import MetricWindow
from models import MetricPoint, Scope, VmResource


class InventoryPort(Protocol):
    async def list_vms(self, scope: Scope) -> list[VmResource]: ...


class MetricsPort(Protocol):
    async def query(
        self,
        region: str,
        subscription_id: str,
        resource_ids: list[str],
        namespace: str,
        metric_name: str,
        window: MetricWindow,
    ) -> dict[str, list[MetricPoint]]: ...


class PricingPort(Protocol):
    async def monthly_price(self, region: str, sku: str, os_type: str) -> Decimal | None: ...
