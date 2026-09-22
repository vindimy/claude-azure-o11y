"""Registry of supported resource types, keyed by the config key in thresholds YAML."""

from __future__ import annotations

from resource_types import cosmos, sqldb, sqlpool, vm, vnet
from resource_types.registry import ResourceTypeSpec

TYPES: dict[str, ResourceTypeSpec] = {
    vm.SPEC.kind: vm.SPEC,
    sqldb.SPEC.kind: sqldb.SPEC,
    sqlpool.SPEC.kind: sqlpool.SPEC,
    cosmos.SPEC.kind: cosmos.SPEC,
    vnet.SPEC.kind: vnet.SPEC,
}
