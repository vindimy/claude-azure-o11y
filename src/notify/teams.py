"""Power Automate Workflows incoming webhook with an Adaptive Card payload."""

from __future__ import annotations

from typing import Any

import httpx

from config.models import TagNames
from models import HotAlert


def build_hot_card(alert: HotAlert, tags: TagNames) -> dict[str, Any]:
    vm = alert.resource
    facts = [
        {"title": "Resource", "value": vm.name},
        {"title": "Subscription", "value": vm.subscription_id},
        {"title": "Resource group", "value": vm.resource_group},
        {"title": "Region", "value": vm.location},
        {"title": "SKU", "value": vm.vm_size},
        {"title": "Metric", "value": alert.metric},
        {
            "title": "Observed",
            "value": f"{alert.observed:g}% avg over {alert.lookback_minutes} min",
        },
        {"title": "Threshold", "value": f"{alert.threshold:g}%"},
    ]
    missing: list[str] = []
    for tag_name in (tags.car_id, tags.owner, tags.assignment_group):
        value = vm.tag(tag_name)
        if value:
            facts.append({"title": tag_name, "value": value})
        else:
            missing.append(tag_name)
    body: list[dict[str, Any]] = [
        {
            "type": "TextBlock",
            "size": "Medium",
            "weight": "Bolder",
            "text": f"HOT: {vm.name} {alert.metric.upper()} above threshold",
        },
        {"type": "FactSet", "facts": facts},
    ]
    if missing:
        body.append(
            {
                "type": "TextBlock",
                "wrap": True,
                "isSubtle": True,
                "text": "Missing tags: " + ", ".join(missing),
            }
        )
    body.append(
        {"type": "TextBlock", "wrap": True, "isSubtle": True, "size": "Small", "text": vm.id}
    )
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "msteams": {"width": "Full"},
                    "body": body,
                },
            }
        ],
    }


class TeamsNotifier:
    def __init__(self, http: httpx.AsyncClient, webhook_url: str, tags: TagNames) -> None:
        self._http, self._url, self._tags = http, webhook_url, tags

    async def send_hot(self, alert: HotAlert) -> None:
        resp = await self._http.post(
            self._url, json=build_hot_card(alert, self._tags), timeout=15.0
        )
        resp.raise_for_status()
