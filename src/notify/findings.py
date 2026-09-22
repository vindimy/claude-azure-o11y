"""Row builders for the Log Analytics findings tables. Pure.

Columns must match schema/findings-tables.json exactly; tests/test_findings.py enforces it.
Values are JSON-native (ISO strings for datetimes, floats for money) so any sink can serialize them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from config.models import AssignmentGroups, MetricThreshold, TagNames
from models import HotAlert, Recommendation, Resource

OPS_TABLE = "O11yOpsFindings_CL"
FINOPS_TABLE = "O11yFinOpsFindings_CL"

Row = dict[str, Any]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class RunContext:
    run_id: str
    mg_id: str
    run_at: datetime
    tags: TagNames
    assignment_groups: AssignmentGroups
    currency: str


@dataclass(frozen=True)
class MetricContext:
    """What was measured and over which window; shared by every row of one metric in a run."""

    namespace: str
    metric_key: str
    metric: MetricThreshold
    aggregation: str
    window_start: datetime
    window_end: datetime
    granularity: str
    """The grain the series was fetched at (ISO 8601), per-type override included."""


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def portal_url(resource_id: str) -> str:
    return f"https://portal.azure.com/#resource{resource_id}/overview"


def _money(v: Decimal | None) -> float | None:
    return None if v is None else float(round(v, 2))


def _resource_columns(r: Resource) -> Row:
    return {
        "ResourceId": r.id,
        "ResourceName": r.name,
        "ResourceType": r.type.lower(),
        "SubscriptionId": r.subscription_id,
        "ResourceGroup": r.resource_group,
        "Location": r.location,
        "Sku": r.sku,
        "PortalUrl": portal_url(r.id),
        "Tags": dict(r.tags),
    }


def ownership_columns(r: Resource, ctx: RunContext) -> Row:
    """Ownership tags plus the tags that are missing, malformed (owner) or unmapped (group)."""
    t = ctx.tags
    owner = r.tag(t.owner) or ""
    group = r.tag(t.assignment_group) or ""
    group_email = ctx.assignment_groups.email_for(group) or ""
    car_id = r.tag(t.car_id) or ""
    missing = [
        name
        for name, ok in (
            (t.owner, bool(_EMAIL.match(owner))),
            (t.assignment_group, bool(group_email)),
            (t.car_id, bool(car_id)),
        )
        if not ok
    ]
    return {
        "Owner": owner,
        "AssignmentGroup": group,
        "AssignmentGroupEmail": group_email,
        "CarId": car_id,
        "MissingTags": missing,
    }


def _common(r: Resource, ctx: RunContext, m: MetricContext) -> Row:
    return {
        "TimeGenerated": iso(ctx.run_at),
        "RunId": ctx.run_id,
        "ManagementGroupId": ctx.mg_id,
        **_resource_columns(r),
        "MetricNamespace": m.namespace,
        "MetricName": m.metric.metric_name,
        "MetricKey": m.metric_key,
        "Unit": m.metric.unit,
        "Aggregation": m.aggregation,
        "WindowStart": iso(m.window_start),
        "WindowEnd": iso(m.window_end),
        **ownership_columns(r, ctx),
    }


def ops_row(alert: HotAlert, ctx: RunContext, m: MetricContext) -> Row:
    return {
        **_common(alert.resource, ctx, m),
        "ObservedValue": alert.observed,
        "Threshold": alert.threshold,
        "ThresholdSource": alert.threshold_source,
        "LookbackMinutes": alert.lookback_minutes,
    }


def finops_row(rec: Recommendation, ctx: RunContext, m: MetricContext) -> Row:
    f = rec.finding
    return {
        **_common(f.resource, ctx, m),
        "OsType": str(f.resource.prop("os_type", "")),
        "Granularity": m.granularity,
        "Percentile": f.percentile,
        "ObservedValue": f.observed,
        "Threshold": f.threshold,
        "ThresholdSource": f.threshold_source,
        "LookbackDays": f.lookback_days,
        "DataCoverage": f.coverage,
        "RecommendedSku": rec.target_sku or "",
        "Confidence": rec.confidence,
        "Reason": rec.reason,
        "CurrentMonthlyCost": _money(rec.current_monthly),
        "ProjectedMonthlyCost": _money(rec.projected_monthly),
        "EstimatedMonthlySaving": _money(rec.saving),
        "Currency": ctx.currency,
    }
