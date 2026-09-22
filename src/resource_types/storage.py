"""Storage accounts: Resource Graph query, parser, readiness check, tiering skip, recommender
binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.storage import StorageRecommendRules, recommend_storage
from resource_types.registry import ResourceTypeSpec, base_resource, require_state

KIND = "storage"
ARM_TYPE = "microsoft.storage/storageaccounts"
SUCCEEDED = "Succeeded"
STANDARD = "standard"
HOT = "hot"
TIERABLE_KINDS = {"storagev2", "blobstorage"}

QUERY = """
resources
| where type =~ 'microsoft.storage/storageaccounts'
| project id, name, subscriptionId, resourceGroup, location, tags, kind,
          skuName = tostring(sku.name), tier = tostring(sku.tier),
          accessTier = tostring(properties.accessTier),
          state = tostring(properties.provisioningState),
          hns = tobool(properties.isHnsEnabled)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    sku_name = str(row.get("skuName") or "")
    tier = str(row.get("tier") or "")
    account_kind = str(row.get("kind") or "")
    access_tier = str(row.get("accessTier") or "")
    tierable = (
        tier.lower() == STANDARD
        and account_kind.lower() in TIERABLE_KINDS
        and access_tier.lower() == HOT
    )
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=f"{sku_name} {access_tier}".strip(),
        props={
            "sku_name": sku_name,
            "tier": tier,
            "account_kind": account_kind,
            "access_tier": access_tier,
            "state": str(row.get("state") or ""),
            "hns": bool(row.get("hns") or False),
            "tierable": tierable,
        },
    )


active = require_state("state", SUCCEEDED, "not_ready")


def finops_skip(resource: Resource) -> Skip | None:
    """Only a Standard StorageV2/BlobStorage account in the Hot tier has a colder tier to go to."""
    if bool(resource.prop("tierable")):
        return None
    access_tier = str(resource.prop("access_tier", ""))
    tier = str(resource.prop("tier", ""))
    if access_tier and access_tier.lower() != HOT:
        detail = access_tier
    elif tier.lower() == "premium":
        detail = tier
    else:
        detail = str(resource.prop("account_kind", ""))
    return Skip(resource.id, "not_tierable", detail)


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_storage(finding, config.rules_for(KIND, StorageRecommendRules))


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    finops_skip=finops_skip,
    recommend=recommend,
    rules_model=StorageRecommendRules,
)
