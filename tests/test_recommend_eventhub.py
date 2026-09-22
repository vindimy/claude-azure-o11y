from __future__ import annotations

import pytest

from models import ColdFinding, Resource
from recommend.eventhub import EventHubRecommendRules, recommend_eventhub, unit_label

_SUFFIX = (
    " Standard→Basic not evaluated (needs capture/consumer-group/retention checks). "
    "Pricing not implemented for Event Hubs."
)


def finding(tier: str, capacity: int, observed: float, percentile: int = 95) -> ColdFinding:
    ns = Resource(
        kind="eventhub",
        id=(
            "/subscriptions/s1/resourceGroups/rg/providers/Microsoft.EventHub/namespaces/"
            f"eh-{tier.lower()}"
        ),
        name=f"eh-{tier.lower()}",
        type="microsoft.eventhub/namespaces",
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        sku=f"{tier} {capacity} {unit_label(tier)}",
        tags={},
        props={"tier": tier, "capacity": capacity},
    )
    return ColdFinding(
        resource=ns,
        metric="ingress",
        observed=observed,
        percentile=percentile,
        threshold=30,
        lookback_days=14,
        coverage=1.0,
    )


def test_recommends_fewer_units_when_headroom_leaves_room() -> None:
    rec = recommend_eventhub(finding("Standard", 4, 20.0), EventHubRecommendRules())
    assert rec.target_sku == "Standard 2 TU"
    assert rec.confidence == "medium"
    assert "P95 ingress 20% of 4 TU over 14d is below 30%" in rec.reason
    assert rec.reason.endswith(_SUFFIX)


def test_premium_unit_label_is_pu() -> None:
    rec = recommend_eventhub(finding("Premium", 4, 20.0), EventHubRecommendRules())
    assert rec.target_sku == "Premium 2 PU"


def test_already_at_smallest_unit_gives_no_target() -> None:
    rec = recommend_eventhub(finding("Premium", 1, 20.0), EventHubRecommendRules())
    assert rec.target_sku is None
    assert rec.confidence == "medium"
    assert "already at the smallest size that covers the ingress" in rec.reason
    assert rec.reason.endswith(_SUFFIX)


def test_target_rounds_up_to_whole_units() -> None:
    # 8 * 0.10 * 1.3 = 1.04 -> ceil to 2, not 1.
    rec = recommend_eventhub(finding("Standard", 8, 10.0), EventHubRecommendRules())
    assert rec.target_sku == "Standard 2 TU"


def test_reason_interpolates_the_configured_percentile() -> None:
    rec = recommend_eventhub(finding("Standard", 4, 20.0, percentile=90), EventHubRecommendRules())
    assert "P90 ingress 20% of 4 TU" in rec.reason
    assert "P95" not in rec.reason


def test_lower_cased_tier_still_gets_pu_units_and_keeps_its_casing() -> None:
    rec = recommend_eventhub(finding("premium", 4, 20.0), EventHubRecommendRules())
    assert rec.target_sku == "premium 2 PU"
    assert "of 4 PU over 14d" in rec.reason


def test_unit_label_is_case_insensitive() -> None:
    assert unit_label("Premium") == "PU" and unit_label("premium") == "PU"
    assert unit_label("Standard") == "TU" and unit_label("basic") == "TU"


def test_rules_reject_unknown_keys() -> None:
    with pytest.raises(ValueError):
        EventHubRecommendRules.model_validate({"headroom": 1.3, "bogus": 2})


def test_default_headroom_is_1_3() -> None:
    assert EventHubRecommendRules().headroom == 1.3
