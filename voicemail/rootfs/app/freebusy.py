"""Busy blocks for the phone agent (0.7.0).

Only event start/end times are read.  Titles, descriptions, locations, uids,
attendees, and which calendar an event came from never leave this module.
Rules: weekdays only, inside the working window (default 08:00-18:00
America/New_York), overlapping events merged, blocks rounded out to 30
minutes, all-day events ignored, at most 14 days, never in the past.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
MAX_DAYS = 14
MAX_AHEAD_DAYS = 14
ROUND_MIN = 30
CAL_RE = re.compile(r"^calendar\.[a-z0-9_]{1,60}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_calendars(raw: Any) -> list[str]:
    out = []
    for part in str(raw or "").split(","):
        name = part.strip()
        if CAL_RE.fullmatch(name) and name not in out:
            out.append(name)
    return out[:5]


def parse_window(raw: Any) -> tuple[time, time]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", str(raw or ""))
    if m:
        a = time(int(m.group(1)) % 24, int(m.group(2)) % 60)
        b = time(int(m.group(3)) % 24, int(m.group(4)) % 60)
        if a < b:
            return a, b
    return time(8, 0), time(18, 0)


def validate_range(start_date: Any, days: Any, today: date) -> tuple[date, int] | str:
    text = str(start_date or "").strip()
    if not text:
        start = today
    elif not _DATE_RE.fullmatch(text):
        return "bad-start-date"
    else:
        try:
            start = date.fromisoformat(text)
        except ValueError:
            return "bad-start-date"
    if start < today:
        return "past-date"
    if (start - today).days > MAX_AHEAD_DAYS:
        return "too-far-ahead"
    try:
        n = int(days if days not in (None, "") else 5)
    except (TypeError, ValueError):
        return "bad-days"
    if n < 1 or n > MAX_DAYS:
        return "bad-days"
    return start, n


def _event_bounds(ev: dict) -> tuple[datetime, datetime] | None:
    """Timed events only.  HA REST returns {"dateTime": ...} or {"date": ...}."""
    if not isinstance(ev, dict):
        return None
    if str(ev.get("status") or "").lower() == "cancelled":
        return None
    s, e = ev.get("start"), ev.get("end")
    if isinstance(s, dict):
        s = s.get("dateTime")
    if isinstance(e, dict):
        e = e.get("dateTime")
    if not isinstance(s, str) or not isinstance(e, str) or "T" not in s or "T" not in e:
        return None
    try:
        a = datetime.fromisoformat(s.replace("Z", "+00:00"))
        b = datetime.fromisoformat(e.replace("Z", "+00:00"))
    except ValueError:
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=TZ)
    if b.tzinfo is None:
        b = b.replace(tzinfo=TZ)
    if b <= a:
        return None
    return a.astimezone(TZ), b.astimezone(TZ)


def _floor(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // ROUND_MIN) * ROUND_MIN, second=0, microsecond=0)


def _ceil(dt: datetime) -> datetime:
    base = _floor(dt)
    return base if base == dt else base + timedelta(minutes=ROUND_MIN)


def busy_blocks(events: Iterable[dict], start: date, days: int, window: tuple[time, time]) -> list[dict]:
    end_day = start + timedelta(days=days)
    spans: list[tuple[datetime, datetime]] = []
    for ev in events:
        bounds = _event_bounds(ev)
        if bounds is None:
            continue
        a, b = bounds
        day = max(a.date(), start)
        while day <= b.date() and day < end_day:
            if day.weekday() < 5:
                w0 = datetime.combine(day, window[0], TZ)
                w1 = datetime.combine(day, window[1], TZ)
                x, y = max(a, w0), min(b, w1)
                if x < y:
                    spans.append((max(_floor(x), w0), min(_ceil(y), w1)))
            day += timedelta(days=1)
    spans.sort()
    merged: list[list[datetime]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [{"start": a.isoformat(timespec="minutes"), "end": b.isoformat(timespec="minutes")} for a, b in merged]


def response(blocks: list[dict], start: date, days: int, window: tuple[time, time]) -> dict:
    return {
        "tz": "America/New_York",
        "from": start.isoformat(),
        "days": days,
        "window": f"{window[0].strftime('%H:%M')}-{window[1].strftime('%H:%M')} weekdays",
        "busy": blocks,
        "note": "Busy times only.  Bryson confirms any meeting himself.",
    }
