"""Azure Retail Prices API (public, unauthenticated). Cached per run. Never fails the run."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
HOURS_PER_MONTH = Decimal(730)
_EXCLUDED_SKU_WORDS = ("spot", "low priority")


def select_monthly_price(items: list[dict[str, Any]], os_type: str) -> Decimal | None:
    want_windows = os_type.lower() == "windows"
    prices: list[Decimal] = []
    for item in items:
        sku_name = str(item.get("skuName", "")).lower()
        if any(w in sku_name for w in _EXCLUDED_SKU_WORDS):
            continue
        is_windows = "windows" in str(item.get("productName", "")).lower()
        if is_windows != want_windows:
            continue
        if str(item.get("type", "Consumption")) != "Consumption":
            continue
        prices.append(Decimal(str(item["unitPrice"])))
    if not prices:
        return None
    return min(prices) * HOURS_PER_MONTH


class RetailPriceClient:
    def __init__(self, http: httpx.AsyncClient, currency: str = "USD") -> None:
        self._http = http
        self._currency = currency
        self._cache: dict[tuple[str, str, str], Decimal | None] = {}

    async def monthly_price(self, region: str, sku: str, os_type: str) -> Decimal | None:
        key = (region.lower(), sku.lower(), os_type.lower())
        if key in self._cache:
            return self._cache[key]
        flt = (
            f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
            f"and armSkuName eq '{sku}' and priceType eq 'Consumption'"
        )
        try:
            resp = await self._http.get(
                RETAIL_PRICES_URL,
                params={"$filter": flt, "currencyCode": self._currency},
                timeout=20.0,
            )
            resp.raise_for_status()
            price = select_monthly_price(resp.json().get("Items", []), os_type)
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning(
                "pricing lookup failed", extra={"region": region, "sku": sku, "error": str(e)}
            )
            price = None
        self._cache[key] = price
        return price
