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


def never_skip(resource: Resource) -> Skip | None:
    """Default `active` / `finops_skip`: every resource is evaluated."""
    return None


def no_enrichment(resource: Resource, rules: BaseModel) -> Resource:
    """Default `enrich`: the parsed resource is used as is."""
    return resource


def require_state(prop: str, expected: str, reason: str) -> Callable[[Resource], Skip | None]:
    """An `active` check: skip with `reason` unless `props[prop]` equals `expected`.

    Case-insensitive, because Resource Graph's casing is not guaranteed; the skip detail keeps the
    state as returned.
    """

    def check(resource: Resource) -> Skip | None:
        state = str(resource.prop(prop, ""))
        if state.lower() == expected.lower():
            return None
        return Skip(resource.id, reason, state or "unknown")

    return check


@dataclass(frozen=True)
class CatalogSource:
    """A SKU catalog under config/ and the pydantic model that validates it."""

    file: str
    model: type[BaseModel]


@dataclass(frozen=True)
class ResourceTypeSpec:
    """Everything the pipeline needs to know about one resource type.

    kind: the key in thresholds YAML and RESOURCE_TYPES.
    query/parse: Resource Graph KQL and the row → Resource mapper.
    enrich: per-run facts that need the type's rules (e.g. a capacity from configured unit rates).
    active: drop a resource before any metrics call (e.g. a deallocated VM).
    finops_skip: drop a resource from FinOps only (e.g. a database inside an elastic pool).
    metric_source: "monitor" fetches platform metrics; "inventory" reads props (VNET).
    recommend/rules_model: FinOps rules engine and its per-MG knobs (`recommend:` in config).
    catalog: the SKU catalog the recommender reads (`AppConfig.catalog_for(kind, model)`).
    priced: whether PricingPort can price this type's SKUs.
    """

    kind: str
    arm_type: str
    query: str
    parse: Callable[[dict[str, Any]], Resource]
    enrich: Callable[[Resource, BaseModel], Resource] = no_enrichment
    active: Callable[[Resource], Skip | None] = never_skip
    finops_skip: Callable[[Resource], Skip | None] = never_skip
    metric_source: Literal["monitor", "inventory"] = "monitor"
    recommend: Recommender | None = None
    rules_model: type[BaseModel] = EmptyRules
    catalog: CatalogSource | None = None
    priced: bool = False


def parse_tags(row: dict[str, Any]) -> dict[str, str]:
    tags = row.get("tags") or {}
    return {str(k): str(v) for k, v in tags.items() if v is not None}


def base_resource(
    row: dict[str, Any], *, kind: str, arm_type: str, sku: str, props: dict[str, Any]
) -> Resource:
    """A Resource from the columns every type's KQL projects.

    Every query projects `id, name, subscriptionId, resourceGroup, location, tags`; the type adds
    its own columns and turns them into `sku` and `props`.
    """
    return Resource(
        kind=kind,
        id=str(row["id"]),
        name=str(row["name"]),
        type=arm_type,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=sku,
        tags=parse_tags(row),
        props=props,
    )
