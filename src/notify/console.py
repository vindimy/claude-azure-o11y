from __future__ import annotations

import json
import logging

from config.models import TagNames
from models import HotAlert
from notify.teams import build_hot_card

log = logging.getLogger(__name__)


class ConsoleNotifier:
    """Dry-run notifier: logs the card instead of posting it."""

    def __init__(self, tags: TagNames) -> None:
        self._tags = tags
        self.sent: list[HotAlert] = []

    async def send_hot(self, alert: HotAlert) -> None:
        self.sent.append(alert)
        log.info("DRY_RUN hot alert", extra={"card": json.dumps(build_hot_card(alert, self._tags))})
