"""0.7.1: list and detail times show America/New_York, not UTC."""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"))

import inbox_store  # noqa: E402


def test_utc_afternoon_shows_eastern_standard_time():
    # CC-WPJQC case: 17:28 UTC on Nov 15 was 12:28 PM Eastern.
    assert inbox_store.format_displays("2026-11-15T17:28:03.000+00:00") == ("11/15/26", "November 15, 2026 at 12:28")


def test_evening_call_keeps_the_eastern_date():
    # 01:30 UTC Nov 16 is 8:30 PM Nov 15 Eastern; the date must not roll forward.
    assert inbox_store.format_displays("2026-11-16T01:30:00Z") == ("11/15/26", "November 15, 2026 at 20:30")


def test_daylight_time():
    assert inbox_store.format_displays("2026-07-04T16:00:00+00:00") == ("7/4/26", "July 4, 2026 at 12:00")


def test_naive_value_is_treated_as_utc():
    assert inbox_store.format_displays("2026-11-15T17:28:03") == ("11/15/26", "November 15, 2026 at 12:28")


def test_offset_value_is_converted():
    assert inbox_store.format_displays("2026-11-15T12:28:00-05:00")[1] == "November 15, 2026 at 12:28"


def test_bad_or_empty():
    assert inbox_store.format_displays("") == ("", "")
    assert inbox_store.format_displays("nope") == ("", "")
