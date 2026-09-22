"""Virtual network subnets: Resource Graph query and parser. No Azure Monitor metrics.

Subnet IP usage is not exposed as a platform metric; it is computed at parse time from the
Resource Graph row (address prefixes and `ipConfigurations` count) and fed through
`metrics.derive.resolve_series` as a single synthetic point (`metric_source="inventory"`). No
recommender: FinOps runs fetch nothing for this type and write no rows.
"""

from __future__ import annotations

from typing import Any

from models import Resource
from resource_types.registry import ResourceTypeSpec, parse_tags

KIND = "vnet"
ARM_TYPE = "microsoft.network/virtualnetworks/subnets"

QUERY = """
resources
| where type =~ 'microsoft.network/virtualnetworks'
| mv-expand subnet = properties.subnets
| project id = tostring(subnet.id), vnetId = id, name = strcat(name, '/', tostring(subnet.name)),
          subscriptionId, resourceGroup, location, tags,
          prefix = tostring(subnet.properties.addressPrefix),
          prefixes = subnet.properties.addressPrefixes,
          ipUsed = array_length(subnet.properties.ipConfigurations),
          delegated = array_length(subnet.properties.delegations) > 0
| order by id asc
"""


def usable_ips(prefixes: list[str]) -> int:
    """Azure reserves 5 addresses per IPv4 prefix; IPv6 prefixes are not sized here."""
    total = 0
    for p in prefixes:
        length = int(p.rsplit("/", 1)[1])
        if ":" in p:  # IPv6 prefixes are not sized; Azure reserves differ
            continue
        total += max(0, 2 ** (32 - length) - 5)
    return total


def parse(row: dict[str, Any]) -> Resource:
    prefixes = row.get("prefixes") or ([row["prefix"]] if row.get("prefix") else [])
    prefixes = [str(p) for p in prefixes]
    ip_usable = usable_ips(prefixes)
    ip_used = int(row.get("ipUsed") or 0)
    utilization = round(ip_used / ip_usable * 100, 2) if ip_usable > 0 else 0.0
    return Resource(
        kind=KIND,
        id=str(row["id"]),
        name=str(row["name"]),
        type=ARM_TYPE,
        subscription_id=str(row["subscriptionId"]),
        resource_group=str(row["resourceGroup"]),
        location=str(row["location"]),
        sku=", ".join(prefixes),
        tags=parse_tags(row),
        props={
            "vnet_id": str(row["vnetId"]),
            "prefixes": prefixes,
            "ip_usable": ip_usable,
            "ip_used": ip_used,
            "utilization_percent": utilization,
            "delegated": bool(row.get("delegated")),
        },
    )


SPEC = ResourceTypeSpec(
    kind=KIND,
    arm_type=ARM_TYPE,
    query=QUERY,
    parse=parse,
    metric_source="inventory",
)
