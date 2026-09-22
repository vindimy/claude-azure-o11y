"""Shared domain types. Frozen dataclasses only; no SDK types leak in here."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class Scope:
    """Where inventory is enumerated: one management group or an explicit subscription list."""

    kind: Literal["management_group", "subscriptions"]
    values: list[str]


@dataclass(frozen=True)
class Resource:
    """Any evaluated Azure resource. Type-specific facts live in `props` (see resource_types/)."""

    kind: str
    id: str
    name: str
    type: str
    subscription_id: str
    resource_group: str
    location: str
    sku: str
    tags: dict[str, str]
    props: dict[str, Any] = field(default_factory=dict)

    def tag(self, name: str) -> str | None:
        for k, v in self.tags.items():
            if k.lower() == name.lower():
                return v
        return None

    def prop(self, name: str, default: Any = None) -> Any:
        return self.props.get(name, default)


@dataclass(frozen=True)
class MetricPoint:
    timestamp: datetime
    value: float | None


@dataclass(frozen=True)
class MetricRequest:
    """One metric name with the aggregation whose value lands in MetricPoint.value."""

    name: str
    aggregation: str


Series = dict[str, list[MetricPoint]]


@dataclass(frozen=True)
class Skip:
    """A resource that was not fully evaluated, and why. Always logged and counted."""

    resource_id: str
    reason: str
    detail: str = ""


ThresholdSource = Literal["config", "tag"]


@dataclass(frozen=True)
class HotAlert:
    resource: Resource
    metric: str
    observed: float
    threshold: float
    lookback_minutes: int
    threshold_source: ThresholdSource = "config"


@dataclass(frozen=True)
class ColdObservation:
    """One FinOps metric of one resource over the window, cold or not."""

    metric: str
    percentile: int
    value: float
    median: float
    threshold: float
    cold: bool
    coverage: float
    threshold_source: ThresholdSource = "config"


@dataclass(frozen=True)
class ColdFinding:
    """The primary FinOps metric is cold, plus every other observation the recommender may use."""

    resource: Resource
    metric: str
    observed: float
    percentile: int
    threshold: float
    lookback_days: int
    coverage: float
    threshold_source: ThresholdSource = "config"
    observations: tuple[ColdObservation, ...] = ()
    inputs: dict[str, float] = field(default_factory=dict)

    def observation(self, key: str) -> ColdObservation | None:
        return next((o for o in self.observations if o.metric == key), None)

    def evidence(self, label: str) -> str:
        """The cold-metric sentence a recommendation's reason opens with."""
        return (
            f"P{self.percentile} {label} {self.observed:g}% over {self.lookback_days}d is below "
            f"{self.threshold:g}%."
        )


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

    def with_pricing(self, current: Decimal | None, projected: Decimal | None) -> Recommendation:
        """A copy with the cost columns filled."""
        return replace(self, current_monthly=current, projected_monthly=projected)

    def with_note(self, note: str) -> Recommendation:
        """A copy with `note` appended to the reason."""
        return replace(self, reason=f"{self.reason} {note}")


@dataclass
class TypeSummary:
    inventory_total: int = 0
    kept: int = 0
    evaluated: int = 0
    findings: int = 0
    chunk_failures: int = 0


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
    excluded: list[Resource] = field(default_factory=list)
    chunk_failures: int = 0
    type_failures: int = 0
    by_type: dict[str, TypeSummary] = field(default_factory=dict)
    destinations: dict[str, str] = field(default_factory=dict)

    def count_skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def for_type(self, kind: str) -> TypeSummary:
        return self.by_type.setdefault(kind, TypeSummary())
