"""Virtual machines: Resource Graph query, parser, running check, recommender binding."""

from __future__ import annotations

from typing import Any

from config.models import AppConfig
from models import ColdFinding, Recommendation, Resource, Skip
from recommend.vm import VmRecommendRules, recommend_vm
from resource_types.registry import ResourceTypeSpec, parse_tags

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
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=str(row.get("vmSize") or ""),
        tags=parse_tags(row),
        props={
            "os_type": str(row.get("osType") or ""),
            "power_state": str(row.get("powerState") or ""),
        },
    )


def active(resource: Resource) -> Skip | None:
    state = str(resource.prop("power_state", ""))
    if state.lower() == RUNNING:
        return None
    return Skip(resource.id, "not_running", state or "unknown")


def recommend(finding: ColdFinding, config: AppConfig) -> Recommendation:
    rules = config.rules_for(KIND)
    assert isinstance(rules, VmRecommendRules)
    return recommend_vm(finding, config.vm_skus, rules)


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    active=active,
    recommend=recommend,
    rules_model=VmRecommendRules,
    priced=True,
)
