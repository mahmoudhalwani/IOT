"""When is a person allowed in? Residents default to "always"; guests can be
limited to a date range, days of the week and hours of the day (OP-02)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class AccessRule:
    valid_from: Optional[datetime] = None    # not before this moment
    valid_until: Optional[datetime] = None   # not after this moment (access expires by itself)
    days: Optional[frozenset] = None         # weekdays allowed, 0 = Monday; None = every day
    start: Optional[time] = None             # daily window start; None = all day
    end: Optional[time] = None               # daily window end (may be past midnight)

    def check(self, now: datetime) -> tuple[bool, str]:
        if self.valid_from and now < self.valid_from:
            return False, "access has not started yet"
        if self.valid_until and now > self.valid_until:
            return False, "access has expired"
        if self.start is not None and self.end is not None:
            t, day = now.time(), now.weekday()
            if self.start <= self.end:
                inside = self.start <= t <= self.end
            else:                            # e.g. 22:00-06:00
                inside = t >= self.start or t <= self.end
                if t <= self.end:
                    day = (day - 1) % 7      # the window belongs to the day it started on
            if not inside:
                return False, "outside allowed hours"
            if self.days is not None and day not in self.days:
                return False, "not allowed on this day"
        elif self.days is not None and now.weekday() not in self.days:
            return False, "not allowed on this day"
        return True, "ok"


def parse_days(text: str) -> frozenset:
    """'mon,wed,fri' -> {0, 2, 4}"""
    out = set()
    for part in text.lower().split(","):
        part = part.strip()[:3]
        if part not in DAY_NAMES:
            raise ValueError(f"unknown day '{part}' (use {','.join(DAY_NAMES)})")
        out.add(DAY_NAMES.index(part))
    return frozenset(out)


def parse_hours(text: str) -> tuple[time, time]:
    """'08:00-18:30' -> (time(8, 0), time(18, 30))"""
    a, b = text.split("-")
    return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())
