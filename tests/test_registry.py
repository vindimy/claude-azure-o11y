"""The shared pieces every resource type is built from (src/resource_types/registry.py)."""

from __future__ import annotations

from typing import Any

from models import Resource, Skip
from resource_types import TYPES
from resource_types.registry import (
    CatalogSource,
    EmptyRules,
    ResourceTypeSpec,
    base_resource,
    never_skip,
    no_enrichment,
    require_state,
)

ROW: dict[str, Any] = {
    "id": "/subscriptions/s1/resourceGroups/RG-App/providers/x/y/r1",
    "name": "r1",
    "subscriptionId": "s1",
    "resourceGroup": "RG-App",
    "location": "eastus",
    "tags": {"owner": "alice@example.com", "empty": None, 7: 8},
}


def test_base_resource_maps_the_shared_columns_and_tags() -> None:
    r = base_resource(ROW, kind="k", arm_type="x/y", sku="S", props={"a": 1})
    assert r == Resource(
        kind="k",
        id=ROW["id"],
        name="r1",
        type="x/y",
        subscription_id="s1",
        resource_group="RG-App",
        location="eastus",
        sku="S",
        tags={"owner": "alice@example.com", "7": "8"},
        props={"a": 1},
    )
    assert base_resource({**ROW, "tags": None}, kind="k", arm_type="t", sku="", props={}).tags == {}


def test_require_state_is_case_insensitive_and_keeps_the_raw_state_in_the_skip() -> None:
    active = require_state("state", "Ready", "not_ready")
    ready = base_resource(ROW, kind="k", arm_type="t", sku="", props={"state": "ready"})
    assert active(ready) is None
    stopped = base_resource(ROW, kind="k", arm_type="t", sku="", props={"state": "Stopped"})
    assert active(stopped) == Skip(ROW["id"], "not_ready", "Stopped")
    missing = base_resource(ROW, kind="k", arm_type="t", sku="", props={})
    assert active(missing) == Skip(ROW["id"], "not_ready", "unknown")


def test_spec_defaults_evaluate_everything_unchanged() -> None:
    spec = ResourceTypeSpec(kind="k", arm_type="t", query="resources", parse=lambda row: _r())
    r = _r()
    assert spec.active is never_skip and spec.active(r) is None
    assert spec.finops_skip is never_skip and spec.finops_skip(r) is None
    assert spec.enrich is no_enrichment and spec.enrich(r, EmptyRules()) is r
    assert spec.catalog is None and spec.recommend is None and spec.priced is False


def test_every_registered_type_declares_a_catalog_only_with_a_recommender() -> None:
    for kind, spec in TYPES.items():
        if spec.catalog is not None:
            assert isinstance(spec.catalog, CatalogSource)
            assert spec.recommend is not None, kind
            assert spec.catalog.file.endswith(".yaml"), kind


def _r() -> Resource:
    return base_resource(ROW, kind="k", arm_type="t", sku="", props={})
