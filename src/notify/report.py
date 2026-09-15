"""FinOps Markdown report rendering. Pure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from config.models import TagNames
from models import Recommendation, VmResource

MISSING = "(missing)"

COLUMNS = [
    "Resource",
    "Subscription",
    "car_id",
    "assignment_group",
    "owner",
    "Current SKU",
    "P95 CPU",
    "Recommended SKU",
    "Current monthly",
    "Projected monthly",
    "Est. saving",
    "Confidence",
    "Reason",
]


@dataclass(frozen=True)
class ReportMeta:
    mg_id: str
    run_at: datetime
    currency: str
    tags: TagNames
    ignored_rg_count: int
    skips: dict[str, int]
    excluded: list[VmResource]
    chunk_failures: int


def report_relative_path(
    run_at: datetime, mg_id: str, resource_type: str = "virtual-machines"
) -> str:
    return f"{run_at:%Y-%m-%d}/{mg_id}/{resource_type}.md"


def _money(v: Decimal | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def _tag(vm: VmResource, name: str) -> str:
    return vm.tag(name) or MISSING


def _row(r: Recommendation, t: TagNames) -> str:
    vm = r.finding.resource
    cells = [
        vm.name,
        vm.subscription_id,
        _tag(vm, t.car_id),
        _tag(vm, t.assignment_group),
        _tag(vm, t.owner),
        vm.vm_size,
        f"{r.finding.observed_p95:g}%",
        r.target_sku or "-",
        _money(r.current_monthly),
        _money(r.projected_monthly),
        _money(r.saving),
        r.confidence,
        r.reason.replace("|", "/"),
    ]
    return "| " + " | ".join(cells) + " |"


def render_vm_report(recs: list[Recommendation], meta: ReportMeta) -> str:
    t = meta.tags
    rows = sorted(
        recs,
        key=lambda r: (
            _tag(r.finding.resource, t.car_id) == MISSING,
            _tag(r.finding.resource, t.car_id),
            r.finding.resource.name,
        ),
    )
    lines: list[str] = [
        f"# FinOps savings report: Virtual Machines ({meta.mg_id})",
        "",
        f"Run: {meta.run_at:%Y-%m-%d %H:%M} UTC - Metric: CPU P95 over the FinOps lookback"
        f" - Currency: {meta.currency}",
        "",
    ]
    if not rows:
        lines += ["No cold VMs found.", ""]
    else:
        lines.append("| " + " | ".join(COLUMNS) + " |")
        lines.append("|" + "---|" * len(COLUMNS))
        lines += [_row(r, t) for r in rows]
        lines.append("")
    total = sum((r.saving for r in rows if r.saving is not None), Decimal(0))
    lines += [
        "## Totals",
        "",
        f"- Cold VMs: {len(rows)}",
        f"- **Total estimated monthly saving:** {total:.2f} {meta.currency}",
        f"- Rows without pricing: {sum(1 for r in rows if r.saving is None)}",
        "",
        "## Tag hygiene",
        "",
    ]
    for tag_name in (t.car_id, t.owner, t.assignment_group):
        missing = sum(1 for r in rows if _tag(r.finding.resource, tag_name) == MISSING)
        lines.append(f"- Missing `{tag_name}`: {missing}")
    lines += ["", "## Excluded resources", ""]
    if meta.excluded:
        lines.append(f"Tagged `{t.exclude}=true`:")
        lines.append("")
        lines += [f"- {vm.name} ({vm.subscription_id}/{vm.resource_group})" for vm in meta.excluded]
    else:
        lines.append("None.")
    lines += ["", "## Run footer", "", f"- Ignored resource groups: {meta.ignored_rg_count}"]
    for reason, count in sorted(meta.skips.items()):
        lines.append(f"- Skipped `{reason}`: {count}  <!-- {reason}: {count} -->")
    lines.append(f"- Metric batches failed: {meta.chunk_failures}")
    lines.append("")
    return "\n".join(lines)
