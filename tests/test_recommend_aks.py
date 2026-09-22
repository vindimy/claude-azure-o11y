from __future__ import annotations

from typing import Any

from config.models import FamilySkuCatalog
from models import ColdFinding, ColdObservation, Resource
from recommend.aks import AksRecommendRules, recommend_aks

VERIFY = "Cluster-level metric; verify per node pool."

CATALOG = FamilySkuCatalog.model_validate(
    {
        "Standard_D2s_v5": {"family": "Dsv5", "vcpu": 2, "memory_gib": 8},
        "Standard_D4s_v5": {"family": "Dsv5", "vcpu": 4, "memory_gib": 16},
        "Standard_D8s_v5": {"family": "Dsv5", "vcpu": 8, "memory_gib": 32},
    }
)


def pool(
    name: str,
    vm_size: str,
    count: int,
    *,
    autoscale: bool = False,
    min_count: int = 0,
    max_count: int = 0,
) -> dict[str, Any]:
    return {
        "name": name,
        "vm_size": vm_size,
        "count": count,
        "mode": "System" if name == "system" else "User",
        "autoscale": autoscale,
        "min_count": min_count,
        "max_count": max_count,
    }


def finding(
    cpu: float,
    mem: float | None = None,
    *,
    pools: list[dict[str, Any]] | None = None,
    unneeded: float | None = None,
) -> ColdFinding:
    pools = pools if pools is not None else []
    cluster = Resource(
        kind="aks",
        id=(
            "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.ContainerService"
            "/managedClusters/aks-cold"
        ),
        name="aks-cold",
        type="microsoft.containerservice/managedclusters",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=" + ".join(f"{p['vm_size']} x{p['count']}" for p in pools),
        tags={},
        props={
            "tier": "Standard",
            "power_state": "Running",
            "k8s_version": "1.30.4",
            "pools": pools,
            "node_count": sum(p["count"] for p in pools),
        },
    )
    observations = [ColdObservation("node_cpu", 95, cpu, cpu / 2, 20, True, 1.0)]
    if mem is not None:
        observations.append(ColdObservation("node_memory", 95, mem, mem / 2, 30, True, 1.0))
    return ColdFinding(
        resource=cluster,
        metric="node_cpu",
        observed=cpu,
        percentile=95,
        threshold=20,
        lookback_days=14,
        coverage=1.0,
        observations=tuple(observations),
        inputs={} if unneeded is None else {"unneeded_nodes": unneeded},
    )


def test_fixed_pool_gets_fewer_nodes() -> None:
    rec = recommend_aks(
        finding(10.0, 15.0, pools=[pool("system", "Standard_D4s_v5", 3)]),
        CATALOG,
        AksRecommendRules(),
    )
    # 3 nodes x 15% x 1.3 = 0.585 -> 1 node.
    assert rec.target_sku == "system: Standard_D4s_v5 x1" and rec.confidence == "low"
    assert "P95 node CPU 10% over 14d is below 20%." in rec.reason
    assert "P95 node working-set memory 15% is below 30%." in rec.reason
    assert "Sized each pool to the cluster peak 15% with headroom 1.3." in rec.reason
    assert rec.reason.endswith(VERIFY)


def test_autoscaling_pool_gets_a_lower_minimum() -> None:
    pools = [pool("userpool", "Standard_D8s_v5", 4, autoscale=True, min_count=3, max_count=6)]
    rec = recommend_aks(finding(10.0, 15.0, pools=pools), CATALOG, AksRecommendRules())
    # The minimum, not the running count, is what the rule lowers: 3 x 15% x 1.3 -> 1.
    assert rec.target_sku == "userpool: min 1"


def test_pool_changes_are_joined_in_profile_order() -> None:
    pools = [
        pool("system", "Standard_D4s_v5", 3),
        pool("userpool", "Standard_D8s_v5", 2, autoscale=True, min_count=2, max_count=5),
    ]
    rec = recommend_aks(finding(20.0, 15.0, pools=pools), CATALOG, AksRecommendRules())
    assert rec.target_sku == "system: Standard_D4s_v5 x1; userpool: min 1"


def test_pool_above_the_floor_whose_sized_count_is_not_lower_has_no_target() -> None:
    rec = recommend_aks(
        finding(19.0, 45.0, pools=[pool("system", "Standard_D4s_v5", 2)]),
        CATALOG,
        AksRecommendRules(),
    )
    # 2 x 45% x 1.3 = 1.17 -> 2 nodes: nothing to remove, and the pool is not at min_nodes, so
    # the smaller-SKU step does not apply either.
    assert rec.target_sku is None
    assert "No pool can shrink below min_nodes=1 / min_vcpu=2." in rec.reason


def test_pool_at_the_floor_gets_a_smaller_node_sku() -> None:
    rec = recommend_aks(
        finding(20.0, 15.0, pools=[pool("system", "Standard_D4s_v5", 1)]),
        CATALOG,
        AksRecommendRules(),
    )
    # 20% x 1.3 x 4 vCPU / 2 vCPU = 52% on the smaller size: fits.
    assert rec.target_sku == "system: Standard_D2s_v5 x1"


def test_pool_at_the_floor_keeps_its_sku_when_the_peak_would_not_fit() -> None:
    rec = recommend_aks(
        finding(19.0, 50.0, pools=[pool("system", "Standard_D4s_v5", 1)]),
        CATALOG,
        AksRecommendRules(),
    )
    # 50% x 1.3 x 4 / 2 = 130% on Standard_D2s_v5: too small.
    assert rec.target_sku is None and rec.confidence == "low"
    assert "No pool can shrink below min_nodes=1 / min_vcpu=2." in rec.reason
    assert rec.reason.endswith(VERIFY)


def test_pool_at_the_floor_with_an_unknown_sku_has_no_target() -> None:
    rec = recommend_aks(
        finding(10.0, 15.0, pools=[pool("gpu", "Standard_NC6", 1)]),
        CATALOG,
        AksRecommendRules(),
    )
    assert rec.target_sku is None and rec.confidence == "low"


def test_smallest_family_member_at_the_floor_has_no_target() -> None:
    rec = recommend_aks(
        finding(10.0, 15.0, pools=[pool("system", "Standard_D2s_v5", 1)]),
        CATALOG,
        AksRecommendRules(),
    )
    assert rec.target_sku is None


def test_cluster_without_pools_has_no_target() -> None:
    rec = recommend_aks(finding(10.0, 15.0), CATALOG, AksRecommendRules())
    assert rec.target_sku is None and rec.confidence == "low"
    assert "No pool can shrink below min_nodes=1 / min_vcpu=2." in rec.reason


def test_pools_with_zero_nodes_are_ignored() -> None:
    pools = [pool("empty", "Standard_D4s_v5", 0), pool("system", "Standard_D4s_v5", 3)]
    rec = recommend_aks(finding(10.0, 15.0, pools=pools), CATALOG, AksRecommendRules())
    assert rec.target_sku == "system: Standard_D4s_v5 x1"


def test_unneeded_nodes_from_the_autoscaler_are_cited() -> None:
    rec = recommend_aks(
        finding(10.0, 15.0, pools=[pool("system", "Standard_D4s_v5", 3)], unneeded=2.0),
        CATALOG,
        AksRecommendRules(),
    )
    assert "Autoscaler reports 2 unneeded nodes." in rec.reason
    zero = recommend_aks(
        finding(10.0, 15.0, pools=[pool("system", "Standard_D4s_v5", 3)], unneeded=0.0),
        CATALOG,
        AksRecommendRules(),
    )
    assert "unneeded" not in zero.reason


def test_missing_memory_is_named_and_confidence_stays_low() -> None:
    rec = recommend_aks(
        finding(10.0, pools=[pool("system", "Standard_D4s_v5", 3)]), CATALOG, AksRecommendRules()
    )
    assert rec.target_sku == "system: Standard_D4s_v5 x1" and rec.confidence == "low"
    assert "Memory not evaluated (no node_memory_working_set_percentage data)." in rec.reason


def test_rules_are_configurable() -> None:
    rules = AksRecommendRules(min_nodes=2, min_vcpu=4, headroom=2.0)
    pools = [pool("system", "Standard_D4s_v5", 3), pool("small", "Standard_D4s_v5", 2)]
    rec = recommend_aks(finding(10.0, 15.0, pools=pools), CATALOG, rules)
    # system: 3 x 15% x 2 = 0.9 -> floor 2. small: at the floor, and min_vcpu 4 blocks D2s_v5.
    assert rec.target_sku == "system: Standard_D4s_v5 x2"


def test_rules_reject_unknown_keys() -> None:
    try:
        AksRecommendRules.model_validate({"min_nodes": 1, "min_pods": 3})
    except ValueError as exc:
        assert "min_pods" in str(exc)
    else:
        raise AssertionError("unknown rule key was accepted")
