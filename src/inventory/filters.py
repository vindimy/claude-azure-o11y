"""Post-inventory filtering. Pure; every drop produces a Skip with a reason."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from config.models import TagNames
from models import Skip, VmResource

RUNNING = "powerstate/running"


@dataclass
class FilterResult:
    kept: list[VmResource] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    excluded: list[VmResource] = field(default_factory=list)
    ignored_rg_count: int = 0


def filter_vms(
    vms: list[VmResource], tags: TagNames, patterns: list[re.Pattern[str]]
) -> FilterResult:
    result = FilterResult()
    ignored_rgs: set[str] = set()
    for vm in vms:
        if any(p.fullmatch(vm.resource_group) for p in patterns):
            ignored_rgs.add(f"{vm.subscription_id}/{vm.resource_group}".lower())
            result.skips.append(Skip(vm.id, "ignored_rg", vm.resource_group))
            continue
        if (vm.tag(tags.exclude) or "").strip().lower() == "true":
            result.excluded.append(vm)
            result.skips.append(Skip(vm.id, "excluded_by_tag", f"{tags.exclude}=true"))
            continue
        if vm.power_state.lower() != RUNNING:
            result.skips.append(Skip(vm.id, "not_running", vm.power_state or "unknown"))
            continue
        result.kept.append(vm)
    result.ignored_rg_count = len(ignored_rgs)
    return result
