"""Post-inventory filtering. Pure; every drop produces a Skip with a reason."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from config.models import TagNames
from models import Resource, Skip


@dataclass
class FilterResult:
    kept: list[Resource] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    excluded: list[Resource] = field(default_factory=list)
    ignored_rg_count: int = 0


def filter_resources(
    resources: list[Resource],
    tags: TagNames,
    patterns: list[re.Pattern[str]],
    active: Callable[[Resource], Skip | None],
) -> FilterResult:
    """Drop ignored RGs, o11y-exclude=true, and whatever the type's `active` check rejects."""
    result = FilterResult()
    ignored_rgs: set[str] = set()
    for r in resources:
        if any(p.fullmatch(r.resource_group) for p in patterns):
            ignored_rgs.add(f"{r.subscription_id}/{r.resource_group}".lower())
            result.skips.append(Skip(r.id, "ignored_rg", r.resource_group))
            continue
        if (r.tag(tags.exclude) or "").strip().lower() == "true":
            result.excluded.append(r)
            result.skips.append(Skip(r.id, "excluded_by_tag", f"{tags.exclude}=true"))
            continue
        skip = active(r)
        if skip is not None:
            result.skips.append(skip)
            continue
        result.kept.append(r)
    result.ignored_rg_count = len(ignored_rgs)
    return result
