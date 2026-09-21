"""Ansible filter: NCRONTAB (the Functions timer format) -> systemd OnCalendar.

`ops_schedule_cron` / `finops_schedule_cron` keep their Function App meaning on the VM: six fields
(second minute hour day month day-of-week), evaluated in UTC. Each field is expanded to its value
set and written back as a list, so steps and ranges (`*/15`, `1-5`, `10-50/20`) need no systemd
syntax.
"""

from __future__ import annotations

from typing import Any

_FIELDS = (  # name, min, max
    ("second", 0, 59),
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 6),
)
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_DAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
_DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _value(token: str, field: str, lo: int, hi: int) -> int:
    t = token.lower()
    if field == "month" and t in _MONTHS:
        return _MONTHS.index(t) + 1
    if field == "weekday" and t in _DAYS:
        return _DAYS.index(t)
    if not t.isdigit():
        raise ValueError(f"{field}: {token!r} is not a number")
    n = int(t)
    if field == "weekday" and n == 7:  # cron's alternate Sunday
        n = 0
    if not lo <= n <= hi:
        raise ValueError(f"{field}: {n} is outside {lo}-{hi}")
    return n


def _expand(expr: str, field: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        base, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step_s and (not step_s.isdigit() or step < 1):
            raise ValueError(f"{field}: bad step in {part!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = _value(a, field, lo, hi), _value(b, field, lo, hi)
            if start > end:
                raise ValueError(f"{field}: range {part!r} runs backwards")
        else:
            start = _value(base, field, lo, hi)
            end = hi if step_s else start  # `a/n` means a, a+n, ... up to the max
        values.update(range(start, end + 1, step))
    return values


def ncrontab_to_oncalendar(expr: str) -> str:
    parts = str(expr).split()
    if len(parts) != 6:
        raise ValueError(
            f"NCRONTAB needs 6 fields (second minute hour day month day-of-week), got {expr!r}"
        )
    sets = {
        name: _expand(p, name, lo, hi) for p, (name, lo, hi) in zip(parts, _FIELDS, strict=True)
    }
    full = {name: sets[name] == set(range(lo, hi + 1)) for name, lo, hi in _FIELDS}
    if not full["day"] and not full["weekday"]:
        # cron ORs a restricted day-of-month with a restricted day-of-week; systemd ANDs them.
        raise ValueError(f"{expr!r}: restricting both day and day-of-week is not supported")

    def fmt(name: str) -> str:
        return "*" if full[name] else ",".join(str(v) for v in sorted(sets[name]))

    date = f"*-{fmt('month')}-{fmt('day')}"
    time = f"{fmt('hour')}:{fmt('minute')}:{fmt('second')}"
    weekday = "" if full["weekday"] else ",".join(_DAY_NAMES[d] for d in sorted(sets["weekday"]))
    return " ".join(p for p in (weekday, date, time, "UTC") if p)


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {"ncrontab_to_oncalendar": ncrontab_to_oncalendar}
