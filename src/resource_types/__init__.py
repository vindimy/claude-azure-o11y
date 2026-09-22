"""Registry of supported resource types, keyed by the config key in thresholds YAML."""

from __future__ import annotations

from resource_types import (
    aks,
    appserviceplan,
    cosmos,
    eventhub,
    postgres,
    redis,
    servicebus,
    sqldb,
    sqlmi,
    sqlpool,
    storage,
    vm,
    vnet,
)
from resource_types.registry import ResourceTypeSpec

TYPES: dict[str, ResourceTypeSpec] = {
    vm.SPEC.kind: vm.SPEC,
    sqldb.SPEC.kind: sqldb.SPEC,
    sqlpool.SPEC.kind: sqlpool.SPEC,
    sqlmi.SPEC.kind: sqlmi.SPEC,
    postgres.SPEC.kind: postgres.SPEC,
    cosmos.SPEC.kind: cosmos.SPEC,
    eventhub.SPEC.kind: eventhub.SPEC,
    vnet.SPEC.kind: vnet.SPEC,
    servicebus.SPEC.kind: servicebus.SPEC,
    appserviceplan.SPEC.kind: appserviceplan.SPEC,
    redis.SPEC.kind: redis.SPEC,
    aks.SPEC.kind: aks.SPEC,
    storage.SPEC.kind: storage.SPEC,
}
