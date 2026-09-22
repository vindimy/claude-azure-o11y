"""AKS node-pool rule. Pure function; contract in docs/agents/recommendations.md.

Node CPU and working-set memory are cluster-level rollups across every node, so the rule applies
the cluster peak to each pool in turn: fewer nodes (a lower autoscaler minimum) where a pool can
shrink, else the next smaller node SKU in the family for a pool already at the floor. Every
recommendation is low confidence.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict

from config.models import FamilySkuCatalog
from models import ColdFinding, Recommendation
from recommend.ladder import next_smaller_in_family

VERIFY = " Cluster-level metric; verify per node pool."


class AksRecommendRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_nodes: int = 1
    min_vcpu: int = 2
    headroom: float = 1.3


def recommend_aks(
    finding: ColdFinding, catalog: FamilySkuCatalog, rules: AksRecommendRules
) -> Recommendation:
    cpu = finding.observation("node_cpu")
    mem = finding.observation("node_memory")
    parts: list[str] = []
    if cpu is not None:
        parts.append(
            f"P{cpu.percentile} node CPU {cpu.value:g}% over {finding.lookback_days}d is below "
            f"{cpu.threshold:g}%."
        )
    if mem is not None:
        parts.append(
            f"P{mem.percentile} node working-set memory {mem.value:g}% is below {mem.threshold:g}%."
        )
    else:
        parts.append("Memory not evaluated (no node_memory_working_set_percentage data).")
    peaks = [o.value for o in (cpu, mem) if o is not None]
    assert peaks, "aks finding without a node_cpu or node_memory observation"
    peak = max(peaks)
    unneeded = finding.inputs.get("unneeded_nodes")
    if unneeded is not None and unneeded > 0:
        parts.append(f"Autoscaler reports {unneeded:g} unneeded nodes.")
    evidence = " ".join(parts)

    pools: list[dict[str, Any]] = finding.resource.prop("pools", []) or []
    changes = [
        change for pool in pools if (change := _pool_change(pool, peak, catalog, rules)) is not None
    ]
    if not changes:
        return Recommendation(
            finding=finding,
            target_sku=None,
            confidence="low",
            reason=(
                f"{evidence} No pool can shrink below min_nodes={rules.min_nodes} / "
                f"min_vcpu={rules.min_vcpu}.{VERIFY}"
            ),
        )
    return Recommendation(
        finding=finding,
        target_sku="; ".join(changes),
        confidence="low",
        reason=(
            f"{evidence} Sized each pool to the cluster peak {peak:g}% with headroom "
            f"{rules.headroom:g}.{VERIFY}"
        ),
    )


def _pool_change(
    pool: dict[str, Any], peak: float, catalog: FamilySkuCatalog, rules: AksRecommendRules
) -> str | None:
    """The change for one pool, or None when it cannot shrink.

    `current` is the autoscaler minimum for autoscaling pools, else the fixed count. A pool that
    can lose nodes gets fewer nodes; a pool already at `min_nodes` gets the next smaller node SKU
    in its family when the peak still fits on it.
    """
    name = str(pool.get("name", ""))
    vm_size = str(pool.get("vm_size", ""))
    autoscale = bool(pool.get("autoscale"))
    current = int(pool.get("min_count" if autoscale else "count", 0))
    if current <= 0:
        return None
    n = max(rules.min_nodes, math.ceil(current * peak / 100 * rules.headroom))
    if n < current:
        return f"{name}: min {n}" if autoscale else f"{name}: {vm_size} x{n}"
    if current != rules.min_nodes:
        return None
    spec = catalog.get(vm_size)
    if spec is None:
        return None
    smaller = next_smaller_in_family(catalog, spec, rules.min_vcpu)
    target = catalog.get(smaller) if smaller else None
    if smaller is None or target is None:
        return None
    if peak * rules.headroom * spec.vcpu / target.vcpu > 100:
        return None
    return f"{name}: {smaller} x{current}"
