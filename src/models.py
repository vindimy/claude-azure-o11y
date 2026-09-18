"""Shared domain types. Frozen dataclasses only; no SDK types leak in here."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class Scope:
    """Where inventory is enumerated: one management group or an explicit subscription list."""

    kind: Literal["management_group", "subscriptions"]
    values: list[str]


@dataclass(frozen=True)
class VmResource:
    id: str
    name: str
    subscription_id: str
    resource_group: str
    location: str
    vm_size: str
    os_type: str
    power_state: str
    tags: dict[str, str]

    def tag(self, name: str) -> str | None:
        for k, v in self.tags.items():
            if k.lower() == name.lower():
                return v
        return None


@dataclass(frozen=True)
class MetricPoint:
    timestamp: datetime
    average: float | None


@dataclass(frozen=True)
class Skip:
    """A resource that was not fully evaluated, and why. Always logged and counted."""

    resource_id: str
    reason: str
    detail: str = ""


ThresholdSource = Literal["config", "tag"]


@dataclass(frozen=True)
class HotAlert:
    resource: VmResource
    metric: str
    observed: float
    threshold: float
    lookback_minutes: int
    threshold_source: ThresholdSource = "config"


@dataclass(frozen=True)
class ColdFinding:
    resource: VmResource
    metric: str
    observed_p95: float
    threshold: float
    lookback_days: int
    coverage: float
    threshold_source: ThresholdSource = "config"


Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class Recommendation:
    finding: ColdFinding
    target_sku: str | None
    confidence: Confidence
    reason: str
    current_monthly: Decimal | None = None
    projected_monthly: Decimal | None = None

    @property
    def saving(self) -> Decimal | None:
        if self.current_monthly is None or self.projected_monthly is None:
            return None
        return self.current_monthly - self.projected_monthly


@dataclass
class RunSummary:
    mg_id: str
    mode: str
    started_at: datetime
    inventory_total: int = 0
    evaluated: int = 0
    findings: int = 0
    rows_written: int = 0
    write_failures: int = 0
    skips: dict[str, int] = field(default_factory=dict)
    ignored_rg_count: int = 0
    excluded: list[VmResource] = field(default_factory=list)
    chunk_failures: int = 0
    destinations: dict[str, str] = field(default_factory=dict)

    def count_skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1
