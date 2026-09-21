from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path

import pytest

_PLUGIN = (
    Path(__file__).resolve().parents[1] / "ansible/roles/o11y_alerting/filter_plugins/ncrontab.py"
)
_spec = importlib.util.spec_from_file_location("ncrontab", _PLUGIN)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
convert: Callable[[str], str] = _mod.ncrontab_to_oncalendar


@pytest.mark.parametrize(
    ("cron", "expected"),
    [
        ("0 */15 * * * *", "*-*-* *:0,15,30,45:0 UTC"),  # Ops default
        ("0 0 6 * * *", "*-*-* 6:0:0 UTC"),  # FinOps default
        ("30 5 */6 * * *", "*-*-* 0,6,12,18:5:30 UTC"),
        ("0 30 9 * * 1-5", "Mon,Tue,Wed,Thu,Fri *-*-* 9:30:0 UTC"),
        ("0 0 0 * * sun,SAT", "Sun,Sat *-*-* 0:0:0 UTC"),
        ("0 0 0 * * 7", "Sun *-*-* 0:0:0 UTC"),
        ("0 0 12 1,15 jan-mar *", "*-1,2,3-1,15 12:0:0 UTC"),
        ("0 10-50/20 * * * *", "*-*-* *:10,30,50:0 UTC"),
        ("0 45/5 * * * *", "*-*-* *:45,50,55:0 UTC"),
    ],
)
def test_converts(cron: str, expected: str) -> None:
    assert convert(cron) == expected


@pytest.mark.parametrize(
    "cron",
    [
        "*/15 * * * *",  # 5-field Unix cron
        "0 61 * * * *",
        "0 0 25 * * *",
        "0 0 0 * * 8",
        "0 */0 * * * *",
        "0 50-10 * * * *",
        "0 0 0 1 * 1",  # day-of-month OR day-of-week has no systemd equivalent
        "0 0 0 * foo *",
    ],
)
def test_rejects(cron: str) -> None:
    with pytest.raises(ValueError):
        convert(cron)
