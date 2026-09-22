"""One ResourceTypeSpec per supported Azure resource type. Pure; no SDK imports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from models import ColdFinding, Recommendation, Resource, Skip

if TYPE_CHECKING:
    from config.models import AppConfig

Recommender = Callable[[ColdFinding, "AppConfig"], Recommendation]


class EmptyRules(BaseModel):
    model_config = ConfigDict(extra="forbid")


def keep(resource: Resource) -> Skip | None:
    return None


@dataclass(frozen=True)
class ResourceTypeSpec:
    """Everything the pipeline needs to know about one resource type.

    kind: the key in thresholds YAML and RESOURCE_TYPES.
    query/parse: Resource Graph KQL and the row → Resource mapper.
    active: drop a resource before any metrics call (e.g. a deallocated VM).
    finops_skip: drop a resource from FinOps only (e.g. a database inside an elastic pool).
    metric_source: "monitor" fetches platform metrics; "inventory" reads props (VNET).
    recommend/rules_model: FinOps rules engine and its per-MG knobs (`recommend:` in config).
    priced: whether PricingPort can price this type's SKUs.
    """

    kind: str
    arm_type: str
    query: str
    parse: Callable[[dict[str, Any]], Resource]
    active: Callable[[Resource], Skip | None] = keep
    finops_skip: Callable[[Resource], Skip | None] = keep
    metric_source: Literal["monitor", "inventory"] = "monitor"
    recommend: Recommender | None = None
    rules_model: type[BaseModel] = EmptyRules
    priced: bool = False


def parse_tags(row: dict[str, Any]) -> dict[str, str]:
    tags = row.get("tags") or {}
    return {str(k): str(v) for k, v in tags.items() if v is not None}
