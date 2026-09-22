"""Cosmos DB accounts: Resource Graph query, parser, serverless skip, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.cosmos import CosmosRecommendRules, recommend_cosmos
from resource_types.registry import ResourceTypeSpec, parse_tags

KIND = "cosmos"
ARM_TYPE = "microsoft.documentdb/databaseaccounts"
SERVERLESS = "serverless"

QUERY = """
resources
| where type =~ 'microsoft.documentdb/databaseaccounts'
| extend caps = properties.capabilities
| project id, name, subscriptionId, resourceGroup, location, tags, kind,
          serverless = tostring(caps) contains 'EnableServerless',
          apiKind = tostring(kind), freeTier = tobool(properties.enableFreeTier)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    capacity_mode = SERVERLESS if bool(row.get("serverless")) else "provisioned"
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=capacity_mode,
        tags=parse_tags(row),
        props={
            "api_kind": str(row.get("apiKind") or ""),
            "capacity_mode": capacity_mode,
            "enable_free_tier": bool(row.get("freeTier")),
        },
    )


def finops_skip(resource: Resource) -> Skip | None:
    """Serverless accounts bill per request: there is no throughput to size down."""
    if str(resource.prop("capacity_mode", "")) == SERVERLESS:
        return Skip(resource.id, "no_capacity_model", SERVERLESS)
    return None


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for(KIND)
    assert isinstance(rules, CosmosRecommendRules)
    return recommend_cosmos(finding, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=CosmosRecommendRules,
)
