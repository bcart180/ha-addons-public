"""Validate a usage snapshot and build the read-only view model the panel renders."""

from __future__ import annotations

import json
import math
import os
from typing import Any

ADDON_VERSION = "0.1.1"
ACTIVITIES = ("Chats", "Routines", "Bot-to-Bot")
SCHEMA_VERSION = 1

SUMMARY = (
    "Your weekly usage limit is shared across apps. This page isolates how "
    "Grok Bot usage is distributed across bots and activity."
)
NOTE_SHARES = (
    "All shares are of total Grok Bot usage, not the weekly usage limit. "
    "The donut groups the remaining bots; the bars retain each reported bot."
)
NOTE_ROUNDING = (
    "Bot totals and activity shares are independently rounded. Activity reported "
    "as less than 1% is not assigned a numeric width; it is retained in the detail below."
)
FLOW_CAPTION = (
    "This week → bots → activity. Focus filters the bars and flow; the donut "
    "remains the full-week overview."
)
FLOW_NOTE = (
    "Ribbons show reported whole percentage shares. Rounded bot-level activity "
    "totals need not equal the overall activity shares in the header."
)
FLOW_OMIT_NOTE = (
    "Bots under 1% are not drawn as ribbons. They are listed under Below 1%."
)
REREAD_LABEL = "Re-read snapshot"
REREAD_RESULT = "Re-read the snapshot file on disk. No usage API was contacted."


class SnapshotError(ValueError):
    """The file is not a schema-version-1 usage snapshot."""


def format_number(value: int | float) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotError("expected a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SnapshotError("expected a finite number")
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _require_str(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f"{key} must be a non-empty string")
    return value


def _require_number(value: Any, field: str, low: float, high: float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SnapshotError(f"{field} must be a number")
    if value < low or value > high:
        raise SnapshotError(f"{field} is outside {low}..{high}")
    return value


def _whole_or_lt1(value: Any, field: str) -> int | None:
    """Return a whole percent, or None when the file says under 1. Never treat that as zero."""
    if value == "<1":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotError(f'{field} must be a whole number or "<1"')
    if value < 1:
        raise SnapshotError(f'{field} must be at least 1 or "<1"')
    return value


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def validate(data: Any) -> dict:
    if not isinstance(data, dict):
        raise SnapshotError("snapshot must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError("schema_version must be 1")
    _require_str(data, "source")
    _require_str(data, "source_tool")
    _require_str(data, "plan")
    _require_str(data, "snapshot_at")
    _require_str(data, "snapshot_label")
    _require_str(data, "timezone")

    week = data.get("week")
    if not isinstance(week, dict):
        raise SnapshotError("week must be an object")
    for key in ("label", "start_date", "end_date", "range_label"):
        if not isinstance(week.get(key), str) or not week[key].strip():
            raise SnapshotError(f"week.{key} must be a non-empty string")

    limit = data.get("weekly_limit")
    if not isinstance(limit, dict):
        raise SnapshotError("weekly_limit must be an object")
    _require_number(limit.get("used_percent"), "weekly_limit.used_percent", 0, 100)
    _require_number(
        limit.get("grok_bot_percentage_points"),
        "weekly_limit.grok_bot_percentage_points",
        0,
        100,
    )
    if limit.get("shared_across_apps") is not True and limit.get("shared_across_apps") is not False:
        raise SnapshotError("weekly_limit.shared_across_apps must be true or false")
    for key in ("resets_at", "resets_label", "until_reset_at_snapshot"):
        if not isinstance(limit.get(key), str) or not limit[key].strip():
            raise SnapshotError(f"weekly_limit.{key} must be a non-empty string")

    extra = data.get("extra_usage")
    if not isinstance(extra, dict):
        raise SnapshotError("extra_usage must be an object")
    if not isinstance(extra.get("comes_from"), str) or not extra["comes_from"].strip():
        raise SnapshotError("extra_usage.comes_from must be a non-empty string")
    if extra.get("dollar_figures_included") is not False:
        raise SnapshotError("schema 1 does not carry dollar figures")

    overall = data.get("activity_overall")
    if not isinstance(overall, dict):
        raise SnapshotError("activity_overall must be an object")
    for key in ("bot_to_bot_percent", "chats_percent", "routines_percent"):
        _require_number(overall.get(key), f"activity_overall.{key}", 0, 100)
    if not isinstance(overall.get("approximate"), bool):
        raise SnapshotError("activity_overall.approximate must be true or false")
    if not isinstance(overall.get("note"), str) or not overall["note"].strip():
        raise SnapshotError("activity_overall.note must be a non-empty string")

    if data.get("activities") != list(ACTIVITIES):
        raise SnapshotError('activities must be ["Chats", "Routines", "Bot-to-Bot"]')

    bots = data.get("bots")
    if not isinstance(bots, list) or not bots:
        raise SnapshotError("bots must be a non-empty list")
    names: set[str] = set()
    for index, bot in enumerate(bots):
        if not isinstance(bot, dict):
            raise SnapshotError(f"bots[{index}] must be an object")
        name = bot.get("name")
        if not isinstance(name, str) or not name.strip():
            raise SnapshotError(f"bots[{index}].name must be a non-empty string")
        if name in names:
            raise SnapshotError(f"duplicate bot name {name}")
        names.add(name)
        _whole_or_lt1(bot.get("share_percent"), f"{name} share_percent")
        _validate_activity(bot.get("activity"), name, bot.get("share_percent"))
    return data


def _validate_activity(activity: Any, name: str, share: Any) -> None:
    if activity is None:
        if share != "<1":
            raise SnapshotError(f"{name} needs an activity split when its share is a whole percent")
        return
    if not isinstance(activity, list) or len(activity) != 3:
        raise SnapshotError(f"{name} activity must be three entries or null")
    for expected, entry in zip(ACTIVITIES, activity, strict=True):
        if not isinstance(entry, dict) or entry.get("name") != expected:
            raise SnapshotError(f"{name} activity must list {', '.join(ACTIVITIES)} in that order")
        _whole_or_lt1(entry.get("percent"), f"{name} {expected}")


def _approx(value: int | float, approximate: bool) -> str:
    number = format_number(value)
    return f"≈{number}%" if approximate else f"{number}%"


def _activity_segments(activity: list[dict] | None) -> list[dict]:
    if activity is None:
        return []
    segments = []
    for entry in activity:
        whole = _whole_or_lt1(entry["percent"], entry["name"])
        segments.append({"name": entry["name"], "percent": whole})
    return segments


def _activity_line(segments: list[dict], no_split: bool) -> str:
    if no_split:
        return "No activity split was in the snapshot."
    parts = []
    for segment in segments:
        if segment["percent"] is None:
            parts.append(f"{segment['name']} <1%")
        else:
            parts.append(f"{segment['name']} {segment['percent']}%")
    return " · ".join(parts)


def _below_line(name: str, share_percent: int | None, segments: list[dict], no_split: bool) -> str | None:
    if no_split and share_percent is None:
        return (
            f"{name} — share of Grok Bot usage: <1%. "
            "No activity split was in the snapshot."
        )
    small = [segment["name"] for segment in segments if segment["percent"] is None]
    if share_percent is None:
        small_text = ", ".join(small)
        if small_text:
            tail = f"{small_text}: <1% each."
            return f"{name} — share of Grok Bot usage: <1%. {tail}"
        return f"{name} — share of Grok Bot usage: <1%."
    if not small:
        return None
    joined = ", ".join(small)
    if len(small) == 1:
        return f"{name} — {joined}: <1%."
    return f"{name} — {joined}: <1% each."


def _donut(bots: list[dict]) -> dict:
    numeric = [(index, bot) for index, bot in enumerate(bots) if bot["share_percent"] is not None]
    ranked = sorted(numeric, key=lambda item: (-item[1]["share_percent"], item[0]))
    top_names = {bot["name"] for _, bot in ranked[:3]}
    top = [bot for bot in bots if bot["name"] in top_names]
    rest = [bot for bot in bots if bot["share_percent"] is not None and bot["name"] not in top_names]
    parts = [{"label": bot["name"], "percent": bot["share_percent"]} for bot in top]
    rest_sum = sum(bot["share_percent"] for bot in rest)
    if len(rest) == 1:
        parts.append({"label": rest[0]["name"], "percent": rest[0]["share_percent"]})
    elif len(rest) > 1:
        parts.append({"label": "All remaining bots", "percent": rest_sum})
    total = sum(part["percent"] for part in parts)
    return {
        "center": f"{total}%",
        "center_caption": "This week",
        "parts": parts,
    }


def provenance_line(provenance: str, data: dict) -> str:
    label = data["snapshot_label"]
    source = data["source"]
    if provenance == "bundled":
        return f"Bundled snapshot · {label} · {source}"
    return f"Snapshot {label} · {source}"


def build_view(data: dict, provenance: str) -> dict:
    data = validate(data)
    limit = data["weekly_limit"]
    overall = data["activity_overall"]
    week = data["week"]
    approximate = overall["approximate"]
    shared = (
        "Shared across apps on the plan"
        if limit["shared_across_apps"]
        else "Not marked as shared across apps"
    )
    bots_out = []
    below = []
    numeric_shares = []
    for bot in data["bots"]:
        share = _whole_or_lt1(bot["share_percent"], bot["name"])
        segments = _activity_segments(bot["activity"])
        no_split = bot["activity"] is None
        if share is not None:
            numeric_shares.append(share)
        row = {
            "name": bot["name"],
            "share_percent": share,
            "share_label": "<1%" if share is None else f"{share}%",
            "segments": segments,
            "activity_line": _activity_line(segments, no_split),
            "no_activity_split": no_split,
        }
        bots_out.append(row)
        line = _below_line(bot["name"], share, segments, no_split)
        if line:
            below.append({"bot": bot["name"], "text": line})

    top = max(bots_out, key=lambda row: (row["share_percent"] is not None, row["share_percent"] or 0))
    top_detail = (
        f"{top['name']} · share of this week's Grok Bot usage"
        if top["share_percent"] is not None
        else f"{top['name']} · share of this week's Grok Bot usage is under 1%"
    )
    if provenance == "bundled":
        where = "No file was present in the add-on data directory, so this view is the bundled initial snapshot."
        detail = "Read from the bundled file shipped with the add-on."
    else:
        where = "This view was read from the snapshot file in the add-on data directory."
        detail = "Read from the snapshot file in the add-on data directory."

    return {
        "provenance": provenance,
        "provenance_line": provenance_line(provenance, data),
        "provenance_detail": detail,
        "kicker": f"{data['plan']} · {week['label']} · {week['range_label']}",
        "title": "Usage",
        "summary": SUMMARY,
        "metrics": [
            {
                "label": "Weekly usage limit used",
                "value": f"{format_number(limit['used_percent'])}%",
                "detail": (
                    f"{shared} · Grok Bot: "
                    f"{format_number(limit['grok_bot_percentage_points'])} percentage points"
                ),
            },
            {
                "label": "Until weekly reset",
                "value": limit["until_reset_at_snapshot"],
                "detail": f"{limit['resets_label']} · at snapshot",
            },
            {
                "label": "Top bot",
                "value": top["share_label"] if top["share_percent"] is not None else "<1%",
                "detail": top_detail,
            },
            {
                "label": "Activity shares",
                "value": _approx(overall["bot_to_bot_percent"], approximate),
                "detail": (
                    f"Bot-to-Bot · Chats {_approx(overall['chats_percent'], approximate)}"
                    f" · Routines {_approx(overall['routines_percent'], approximate)}. "
                    f"{overall['note']}"
                ),
            },
        ],
        "donut": _donut(bots_out),
        "notes": [NOTE_SHARES, NOTE_ROUNDING],
        "activities": list(ACTIVITIES),
        "bots": bots_out,
        "below": below,
        "flow_caption": FLOW_CAPTION,
        "flow_note": FLOW_NOTE,
        "flow_omit_note": FLOW_OMIT_NOTE,
        "context": [
            (
                f"Extra usage comes from {data['extra_usage']['comes_from']}. "
                "This snapshot does not include dollar figures or a remaining-credit balance."
            ),
            "This is a saved snapshot, not a live meter.",
            "Until weekly reset is the time remaining when the snapshot was taken. It does not count down.",
            "Re-read snapshot only reads the snapshot file again. It does not contact Grok Bot or any usage API.",
            f"Recorded source tool: {data['source_tool']}.",
            where,
            f"Add-on {ADDON_VERSION} · schema version {data['schema_version']}.",
        ],
        "reread_label": REREAD_LABEL,
        "reread_result": REREAD_RESULT,
        "schema_version": data["schema_version"],
        "whole_share_total": sum(numeric_shares),
    }


def inside_directory(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    base = os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)
