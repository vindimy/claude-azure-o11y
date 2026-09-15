from __future__ import annotations

import json

import httpx
import pytest

from config.models import TagNames
from models import HotAlert, VmResource
from notify.teams import TeamsNotifier, build_hot_card


def alert(tags: dict[str, str]) -> HotAlert:
    vm = VmResource(
        id="/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        name="vm1",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        vm_size="Standard_D2s_v5",
        os_type="Linux",
        power_state="PowerState/running",
        tags=tags,
    )
    return HotAlert(vm, "cpu", 97.3, 90, 60)


def test_card_shape_and_facts() -> None:
    card = build_hot_card(alert({"car_id": "7", "owner": "o@x.com"}), TagNames())
    assert card["type"] == "message"
    content = card["attachments"][0]["content"]
    assert content["type"] == "AdaptiveCard"
    text = json.dumps(content)
    assert "vm1" in text and "97.3" in text and "90" in text and "Standard_D2s_v5" in text
    assert "o@x.com" in text and '"7"' in text
    assert "Missing tags: assignment_group" in text


def test_card_without_missing_tags_line() -> None:
    tags = {"car_id": "7", "owner": "o@x.com", "assignment_group": "g"}
    card = build_hot_card(alert(tags), TagNames())
    assert "Missing tags" not in json.dumps(card)


async def test_notifier_posts_card() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(202)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    n = TeamsNotifier(http, "https://hook", TagNames())
    await n.send_hot(alert({}))
    assert seen[0].method == "POST" and json.loads(seen[0].content)["type"] == "message"


async def test_notifier_raises_on_failure() -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(400)))
    n = TeamsNotifier(http, "https://hook", TagNames())
    with pytest.raises(httpx.HTTPStatusError):
        await n.send_hot(alert({}))
