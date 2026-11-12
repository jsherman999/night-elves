"""Parse "run at" times: 01:30, 1:30am, 2pm, or 2026-11-12 01:30, in local time."""
from __future__ import annotations
import re
from datetime import datetime, timedelta

CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?m?\.?"
_TIME = re.compile(rf"^{CLOCK}$", re.I)
_DATETIME = re.compile(rf"^(\d{{4}})-(\d{{2}})-(\d{{2}})[ T]{CLOCK}$", re.I)

def _hm(h, m, ampm) -> tuple[int, int]:
    h, m = int(h), int(m or 0)
    if ampm:
        if not 1 <= h <= 12:
            raise ValueError("with am/pm the hour must be 1-12")
        h = h % 12 + (12 if ampm.lower() == "p" else 0)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("not a valid time of day")
    return h, m

def label(utc_iso: str) -> str:
    """'Thu 01:30' in local time, for a stored run_after."""
    return f"{datetime.fromisoformat(utc_iso.replace('Z', '+00:00')).astimezone():%a %H:%M}"

def parse_at(text: str, now: datetime) -> datetime:
    """A bare time means its next occurrence after now. Needs a colon or am/pm (or a date)."""
    s = " ".join(text.strip().split())
    if d := _DATETIME.match(s):
        y, mo, day, h, m, ap = d.groups()
        h, m = _hm(h, m, ap)
        return datetime(int(y), int(mo), int(day), h, m, tzinfo=now.tzinfo)
    t = _TIME.match(s)
    if not t or not (t.group(2) or t.group(3)):
        raise ValueError(f"can't read {text!r} as a time; use 01:30, 1:30am or 2026-11-12 01:30")
    h, m = _hm(*t.groups())
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)
