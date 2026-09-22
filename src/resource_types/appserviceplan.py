"""App Service Plans: Resource Graph query, parser, ready check, recommender binding.

Consumption (`Dynamic`) and Flex Consumption plans are excluded in the query: they have no
instances to size. Free and Shared plans are inventoried for Ops but skipped from FinOps: there is
nothing below them.
"""

from __future__ import annotations

from typing import Any

from config.models import AppConfig, FamilySkuCatalog
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.appserviceplan import AppServicePlanRecommendRules, recommend_appserviceplan
from resource_types.registry import CatalogSource, ResourceTypeSpec, base_resource, require_state

KIND = "appserviceplan"
ARM_TYPE = "microsoft.web/serverfarms"
READY = "Ready"
NO_CAPACITY_TIERS = ("free", "shared")

QUERY = """
resources
| where type =~ 'microsoft.web/serverfarms'
| where tolower(sku.tier) !in ('dynamic', 'flexconsumption')
| project id, name, subscriptionId, resourceGroup, location, tags, kind,
          skuName = tostring(sku.name), tier = tostring(sku.tier),
          capacity = toint(sku.capacity),
          status = tostring(properties.status),
          sites = toint(properties.numberOfSites),
          elastic = tobool(properties.elasticScaleEnabled),
          reserved = tobool(properties.reserved)
| order by id asc
"""


def _sku(sku_name: str, instances: int) -> str:
    """`Sku` column for a plan: the SKU name and the instance count."""
    if not sku_name:
        return ""
    return f"{sku_name} x{instances}"


def parse(row: dict[str, Any]) -> Resource:
    sku_name = str(row.get("skuName") or "")
    instances = int(row.get("capacity") or 0)
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=_sku(sku_name, instances),
        props={
            "sku_name": sku_name,
            "tier": str(row.get("tier") or ""),
            "instances": instances,
            "status": str(row.get("status") or ""),
            "sites": int(row.get("sites") or 0),
            "elastic_scale": bool(row.get("elastic") or False),
            "os": "linux" if bool(row.get("reserved") or False) else "windows",
            "plan_kind": str(row.get("kind") or ""),
        },
    )


active = require_state("status", READY, "not_ready")


def finops_skip(resource: Resource) -> Skip | None:
    tier = str(resource.prop("tier", ""))
    if tier.lower() in NO_CAPACITY_TIERS:
        return Skip(resource.id, "no_capacity_model", tier)
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_appserviceplan(
        finding,
        config.catalog_for(KIND, FamilySkuCatalog),
        config.rules_for(KIND, AppServicePlanRecommendRules),
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=AppServicePlanRecommendRules,
    catalog=CatalogSource("appservice-skus.yaml", FamilySkuCatalog),
    priced=False,
)
