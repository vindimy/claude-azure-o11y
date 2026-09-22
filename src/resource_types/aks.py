"""AKS clusters: Resource Graph query, parser, running check, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig, FamilySkuCatalog
from models import ColdFinding, Recommendation, Resource
from recommend.aks import AksRecommendRules, recommend_aks
from resource_types.registry import CatalogSource, ResourceTypeSpec, base_resource, require_state

KIND = "aks"
ARM_TYPE = "microsoft.containerservice/managedclusters"
RUNNING = "Running"

QUERY = """
resources
| where type =~ 'microsoft.containerservice/managedclusters'
| project id, name, subscriptionId, resourceGroup, location, tags,
          tier = tostring(sku.tier),
          powerState = tostring(properties.powerState.code),
          k8sVersion = tostring(properties.kubernetesVersion),
          pools = properties.agentPoolProfiles
| order by id asc
"""


def parse_pool(profile: dict[str, Any]) -> dict[str, Any]:
    """One agent pool profile, read defensively: a missing key is "", 0, or False."""
    return {
        "name": str(profile.get("name") or ""),
        "vm_size": str(profile.get("vmSize") or ""),
        "count": int(profile.get("count") or 0),
        "mode": str(profile.get("mode") or ""),
        "autoscale": bool(profile.get("enableAutoScaling")),
        "min_count": int(profile.get("minCount") or 0),
        "max_count": int(profile.get("maxCount") or 0),
    }


def parse(row: dict[str, Any]) -> Resource:
    pools = [parse_pool(p) for p in (row.get("pools") or []) if isinstance(p, dict)]
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=" + ".join(f"{p['vm_size']} x{p['count']}" for p in pools),
        props={
            "tier": str(row.get("tier") or ""),
            "power_state": str(row.get("powerState") or ""),
            "k8s_version": str(row.get("k8sVersion") or ""),
            "pools": pools,
            "node_count": sum(p["count"] for p in pools),
        },
    )


active = require_state("power_state", RUNNING, "not_running")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_aks(
        finding,
        config.catalog_for(KIND, FamilySkuCatalog),
        config.rules_for(KIND, AksRecommendRules),
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=AksRecommendRules,
    catalog=CatalogSource("vm-skus.yaml", FamilySkuCatalog),
    priced=False,
)
