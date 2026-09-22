"""Registry of supported resource types, keyed by the config key in thresholds YAML."""

from __future__ import annotations

from resource_types import sqlmi, vm
from resource_types.registry import ResourceTypeSpec

TYPES: dict[str, ResourceTypeSpec] = {vm.SPEC.kind: vm.SPEC, sqlmi.SPEC.kind: sqlmi.SPEC}
