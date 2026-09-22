"""Pydantic models for every file under config/. Unknown keys are errors."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, RootModel, field_validator, model_validator

from models import Resource


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_regex(pattern: str) -> None:
    """re.error is not a ValueError, so pydantic would not report it as a validation error."""
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"invalid regex {pattern!r}: {e}") from e


class TagNames(_Strict):
    exclude: str = "o11y-exclude"
    threshold_prefix: str = "o11y-threshold-"
    owner: str = "owner"
    assignment_group: str = "assignment_group"
    car_id: str = "car_id"


class OpsWindow(_Strict):
    lookback_minutes: int = 60
    granularity: str = "PT1M"
    aggregation: str = "average"


class FinopsWindow(_Strict):
    lookback_days: int = 14
    granularity: str = "PT1H"
    aggregation: str = "average"
    percentile: int = 95
    min_coverage: float = 0.5


class Windows(_Strict):
    ops: OpsWindow = OpsWindow()
    finops: FinopsWindow = FinopsWindow()


class Granularity(_Strict):
    """Optional per-type override of windows.<mode>.granularity (ISO 8601 durations)."""

    ops: str | None = None
    finops: str | None = None


class MetricThreshold(_Strict):
    """One metric of one resource type. See docs/agents/thresholds.md for every field."""

    metric_name: str
    unit: str = "Percent"
    aggregation: str = "Average"
    hot_when: Literal["above", "below"] = "above"
    reduce: Literal["mean", "max", "sum"] = "mean"
    ops_hot: float | None = None
    finops_cold: float | None = None
    applies_to: dict[str, list[str]] = {}
    derive: str | None = None
    inputs: list[str] = []
    capacity_prop: str | None = None

    def applies(self, resource: Resource) -> bool:
        return all(
            str(resource.prop(prop, "")).lower() in {v.lower() for v in values}
            for prop, values in self.applies_to.items()
        )


class ResourceTypeThresholds(_Strict):
    namespace: str
    granularity: Granularity = Granularity()
    metrics: dict[str, MetricThreshold]
    recommend: dict[str, Any] = {}

    def ops_metrics(self) -> dict[str, MetricThreshold]:
        return {k: m for k, m in self.metrics.items() if m.ops_hot is not None}

    def finops_metrics(self) -> dict[str, MetricThreshold]:
        return {k: m for k, m in self.metrics.items() if m.finops_cold is not None}

    def input_metrics(self) -> dict[str, MetricThreshold]:
        """Metrics fetched for the recommender only (no threshold on either side)."""
        return {
            k: m for k, m in self.metrics.items() if m.ops_hot is None and m.finops_cold is None
        }

    def primary_finops_key(self) -> str | None:
        return next(iter(self.finops_metrics()), None)

    @model_validator(mode="after")
    def _one_aggregation_per_raw_metric(self) -> ResourceTypeThresholds:
        """One raw metric name per run mode may ask for only one aggregation.

        `metrics.derive.requests_for` de-duplicates the batch call by raw metric name, so a second
        key naming the same metric with a different aggregation would be silently evaluated
        against the first key's series. Fail at startup instead.
        """
        for mode in (self.ops_metrics(), {**self.finops_metrics(), **self.input_metrics()}):
            seen: dict[str, tuple[str, str]] = {}
            for key, m in mode.items():
                for name in m.inputs if m.derive else [m.metric_name]:
                    first_key, first_agg = seen.setdefault(name, (key, m.aggregation))
                    if first_agg != m.aggregation:
                        raise ValueError(
                            f"metric {name!r} is requested with two aggregations in one run "
                            f"mode: {first_key}={first_agg} and {key}={m.aggregation}"
                        )
        return self


class Thresholds(_Strict):
    version: int = 1
    tags: TagNames = TagNames()
    windows: Windows = Windows()
    resource_types: dict[str, ResourceTypeThresholds]


class IgnoreConfig(_Strict):
    resource_groups: list[str] = []
    per_mg: dict[str, list[str]] = {}

    @field_validator("resource_groups")
    @classmethod
    def _compile_global(cls, v: list[str]) -> list[str]:
        for p in v:
            _check_regex(p)
        return v

    @field_validator("per_mg")
    @classmethod
    def _compile_per_mg(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for pats in v.values():
            for p in pats:
                _check_regex(p)
        return v

    def patterns_for(self, mg_id: str) -> list[re.Pattern[str]]:
        raw = [*self.resource_groups, *self.per_mg.get(mg_id, [])]
        return [re.compile(p, re.IGNORECASE) for p in raw]


class AssignmentGroup(_Strict):
    email: EmailStr


class AssignmentGroups(RootModel[dict[str, AssignmentGroup]]):
    def email_for(self, group: str | None) -> str | None:
        if not group:
            return None
        for name, spec in self.root.items():
            if name.lower() == group.lower():
                return str(spec.email)
        return None


class VmSku(_Strict):
    family: str
    vcpu: int
    memory_gib: float


class VmSkuCatalog(RootModel[dict[str, VmSku]]):
    """Family ladders keyed by SKU name: vm-skus.yaml for VMs, postgres-skus.yaml for PostgreSQL."""

    def get(self, sku: str) -> VmSku | None:
        for name, spec in self.root.items():
            if name.lower() == sku.lower():
                return spec
        return None

    def canonical_name(self, sku: str) -> str | None:
        for name in self.root:
            if name.lower() == sku.lower():
                return name
        return None

    def family_members(self, family: str) -> list[tuple[str, VmSku]]:
        members = [(n, s) for n, s in self.root.items() if s.family == family]
        return sorted(members, key=lambda item: (item[1].vcpu, item[1].memory_gib))


class SqlSkuCatalog(_Strict):
    """config/sql-skus.yaml: DTU service objectives, pool eDTU sizes, vCore ladders."""

    dtu: dict[str, dict[str, int]] = {}
    pool_edtu: dict[str, list[int]] = {}
    vcore: dict[str, list[int]] = {}


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thresholds: Thresholds
    ignore: IgnoreConfig
    assignment_groups: AssignmentGroups
    vm_skus: VmSkuCatalog
    postgres_skus: VmSkuCatalog = VmSkuCatalog({})
    sql_skus: SqlSkuCatalog = SqlSkuCatalog()
    rules: dict[str, BaseModel] = {}

    def rules_for(self, kind: str) -> BaseModel:
        return self.rules[kind]
