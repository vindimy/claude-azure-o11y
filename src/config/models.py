"""Pydantic models for every file under config/. Unknown keys are errors."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, EmailStr, RootModel, field_validator


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


class MetricThreshold(_Strict):
    metric_name: str
    unit: str = "Percent"
    ops_hot: float
    finops_cold: float


class VmRecommendRules(_Strict):
    min_vcpu: int = 1


class VmThresholds(_Strict):
    namespace: str
    metrics: dict[str, MetricThreshold]
    recommend: VmRecommendRules = VmRecommendRules()


class ResourceTypes(_Strict):
    vm: VmThresholds


class Thresholds(_Strict):
    version: int = 1
    tags: TagNames = TagNames()
    windows: Windows = Windows()
    resource_types: ResourceTypes


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


class AppConfig(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thresholds: Thresholds
    ignore: IgnoreConfig
    assignment_groups: AssignmentGroups
    vm_skus: VmSkuCatalog
