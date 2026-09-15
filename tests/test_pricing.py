from __future__ import annotations

from decimal import Decimal

import httpx

from recommend.pricing import RetailPriceClient, select_monthly_price
from tests.conftest import load_fixture


def test_select_linux_skips_spot_and_windows() -> None:
    items = load_fixture("retail_prices_d4s_v5_eastus.json")["Items"]
    assert select_monthly_price(items, "Linux") == Decimal("0.192") * 730


def test_select_windows_price() -> None:
    items = load_fixture("retail_prices_d4s_v5_eastus.json")["Items"]
    assert select_monthly_price(items, "Windows") == Decimal("0.376") * 730


def test_select_none_when_no_match() -> None:
    assert select_monthly_price([], "Linux") is None


async def test_client_builds_filter_and_caches() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=load_fixture("retail_prices_d4s_v5_eastus.json"))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = RetailPriceClient(http)
    price = await client.monthly_price("eastus", "Standard_D4s_v5", "Linux")
    again = await client.monthly_price("eastus", "Standard_D4s_v5", "Linux")
    assert price == again == Decimal("0.192") * 730
    assert len(calls) == 1
    flt = httpx.URL(calls[0]).params["$filter"]
    assert "armSkuName eq 'Standard_D4s_v5'" in flt
    assert "armRegionName eq 'eastus'" in flt
    assert httpx.URL(calls[0]).params["currencyCode"] == "USD"


async def test_client_returns_none_on_http_error() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    client = RetailPriceClient(http)
    assert await client.monthly_price("eastus", "Standard_D4s_v5", "Linux") is None
