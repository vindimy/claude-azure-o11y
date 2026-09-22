"""Virtual machines: Resource Graph query, parser, running check, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig, FamilySkuCatalog
from models import ColdFinding, Recommendation, Resource
from recommend.vm import VmRecommendRules, recommend_vm
from resource_types.registry import CatalogSource, ResourceTypeSpec, base_resource, require_state

KIND = "vm"
ARM_TYPE = "microsoft.compute/virtualmachines"
RUNNING = "powerstate/running"

QUERY = """
resources
| where type =~ 'microsoft.compute/virtualmachines'
| project id, name, subscriptionId, resourceGroup, location, tags,
          vmSize = tostring(properties.hardwareProfile.vmSize),
          osType = tostring(properties.storageProfile.osDisk.osType),
          powerState = tostring(properties.extended.instanceView.powerState.code)
| order by id asc
"""


def parse(row: dict[str, Any]) -> Resource:
    return base_resource(
        row,
        kind=KIND,
        arm_type=ARM_TYPE,
        sku=str(row.get("vmSize") or ""),
        props={
            "os_type": str(row.get("osType") or ""),
            "power_state": str(row.get("powerState") or ""),
        },
    )


active = require_state("power_state", RUNNING, "not_running")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    return recommend_vm(
        finding,
        config.catalog_for(KIND, FamilySkuCatalog),
        config.rules_for(KIND, VmRecommendRules),
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=VmRecommendRules,
    catalog=CatalogSource("vm-skus.yaml", FamilySkuCatalog),
    priced=True,
)
