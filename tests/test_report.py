from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from config.models import TagNames
from models import ColdFinding, Recommendation, VmResource
from notify.report import ReportMeta, render_vm_report, report_relative_path

RUN_AT = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)


def vm(name: str, tags: dict[str, str]) -> VmResource:
    return VmResource(
        id=f"/subscriptions/s1/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/{name}",
        name=name,
        subscription_id="s1",
        resource_group="rg",
        location="eastus",
        vm_size="Standard_D8s_v5",
        os_type="Linux",
        power_state="PowerState/running",
        tags=tags,
    )


def rec(name: str, tags: dict[str, str], saving: bool = True) -> Recommendation:
    f = ColdFinding(vm(name, tags), "cpu", 7.2, 20, 14, 1.0)
    return Recommendation(
        finding=f,
        target_sku="Standard_D4s_v5",
        confidence="medium",
        reason="why",
        current_monthly=Decimal("280.32") if saving else None,
        projected_monthly=Decimal("140.16") if saving else None,
    )


def meta(**kw: Any) -> ReportMeta:
    base: dict[str, Any] = dict(
        mg_id="mg-prod",
        run_at=RUN_AT,
        currency="USD",
        tags=TagNames(),
        ignored_rg_count=3,
        skips={"not_running": 2},
        excluded=[],
        chunk_failures=0,
    )
    base.update(kw)
    return ReportMeta(**base)


def test_path_layout() -> None:
    assert report_relative_path(RUN_AT, "mg-prod") == "2026-09-15/mg-prod/virtual-machines.md"


def test_row_columns_and_totals() -> None:
    tags = {"car_id": "123", "owner": "a@x.com", "assignment_group": "cloud-engineering"}
    md = render_vm_report([rec("vm-a", tags)], meta())
    assert "| vm-a |" in md
    assert "| 123 |" in md and "| a@x.com |" in md and "| cloud-engineering |" in md
    assert "| Standard_D8s_v5 |" in md and "| Standard_D4s_v5 |" in md
    assert "7.2%" in md
    assert "280.32" in md and "140.16" in md
    assert "**Total estimated monthly saving:** 140.16 USD" in md
    assert "why" in md


def test_rows_grouped_by_car_id_and_missing_tags_visible() -> None:
    md = render_vm_report([rec("vm-b", {}), rec("vm-a", {"car_id": "42"})], meta())
    assert md.index("| 42 |") < md.index("| (missing) |")
    assert "Missing `car_id`: 1" in md
    assert "Missing `owner`: 2" in md
    assert "Missing `assignment_group`: 2" in md


def test_footer_counts_and_excluded_appendix() -> None:
    md = render_vm_report([], meta(excluded=[vm("vm-x", {"o11y-exclude": "true"})]))
    assert "Ignored resource groups: 3" in md
    assert "not_running: 2" in md
    assert "## Excluded resources" in md and "vm-x" in md
    assert "No cold VMs found" in md


def test_na_when_price_unknown() -> None:
    md = render_vm_report([rec("vm-a", {}, saving=False)], meta())
    assert "| n/a |" in md
    assert "**Total estimated monthly saving:** 0.00 USD" in md
