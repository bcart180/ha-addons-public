"""Validate a personal sports snapshot. No network and no X client."""

import datetime as dt
import json
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
MAX_BYTES = 262144
MAX_ITEMS = 75
OLDER_SECONDS = 72 * 3600
FUTURE_SKEW = dt.timedelta(minutes=5)
CADENCE_LABEL = (
    "Proposed collector, not scheduled: every 30 minutes from 7:00 AM to 11:00 PM ET, "
    "and hourly from 11:00 PM to 7:00 AM ET."
)
POLL_LABEL = "This page re-reads the snapshot every 60 seconds. That re-read is not a new collection."
ATTRIBUTION = (
    "Official accounts and crosslinked reporters only. A reporter line is attributed, "
    "not treated as a separate fact check. Rumors are not published as fact."
)
TEAM_ORDER = ("Seattle Seahawks", "Navy Midshipmen", "Gonzaga Bulldogs")
TEAM_SPORT = {
    "Seattle Seahawks": "football",
    "Navy Midshipmen": "football",
    "Gonzaga Bulldogs": "basketball",
}
# Numeric ids pinned from the Nov 8, 2026 starter snapshot.
# ZagMBB and BWagner_CapGaz are allowlisted by handle; that snapshot did not include their ids.
AUTHORS = {
    "seahawks": {"handle": "Seahawks", "team": "Seattle Seahawks", "role": "official", "author_id": "23642374"},
    "navyfb": {"handle": "NavyFB", "team": "Navy Midshipmen", "role": "official", "author_id": "2429051735"},
    "zagmbb": {"handle": "ZagMBB", "team": "Gonzaga Bulldogs", "role": "official", "author_id": None},
    "theolawson_sr": {"handle": "TheoLawson_SR", "team": "Gonzaga Bulldogs", "role": "reporter", "author_id": "291955806"},
    "bwagner_capgaz": {"handle": "BWagner_CapGaz", "team": "Navy Midshipmen", "role": "reporter", "author_id": None},
}
REJECTED_HANDLES = {
    "bcondotta": "Bob Condotta is not allowlisted until a publisher page crosslinks the handle.",
}
DOC_REQUIRED = {"schema_version", "collected_at", "display_timezone", "status", "items", "warnings"}
DOC_OPTIONAL = {"query_end_time", "last_attempt_at", "last_success_at", "teams", "source_error"}
ITEM_REQUIRED = {
    "id", "team", "sport", "canonical_post_url", "author_handle", "author_name", "author_id",
    "published_at", "collected_at", "summary", "source_provenance", "source_text", "status",
}
ITEM_OPTIONAL = {"evidence_files"}
DOC_STATUS = {"starter_snapshot_not_live", "ok", "empty", "partial", "error"}
ITEM_STATUS = {"fresh", "older"}
HOSTS = {"x.com", "twitter.com"}


class ValidationError(ValueError):
    pass


def planned_seconds(now):
    local = now.astimezone(ET)
    if 7 <= local.hour < 23:
        return 30 * 60
    return 60 * 60


def format_et(instant):
    local = instant.astimezone(ET)
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%b')} {local.day}, {local.year}, {hour}:{local.minute:02d} {ampm} ET"


def parse_time(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(field + " must be an ISO-8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(field + " must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValidationError(field + " must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValidationError("duplicate key")
        if not isinstance(key, str):
            raise ValidationError("JSON keys must be strings")
        obj[key] = value
    return obj


def load_bytes(raw, now):
    if not isinstance(raw, (bytes, bytearray)):
        raise ValidationError("snapshot must be bytes")
    if len(raw) > MAX_BYTES:
        raise ValidationError("snapshot is larger than 262144 bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValidationError("snapshot must be UTF-8 JSON") from None
    if "\x00" in text:
        raise ValidationError("snapshot must be UTF-8 JSON")
    try:
        data = json.loads(text, object_pairs_hook=_pairs)
    except ValidationError:
        raise
    except json.JSONDecodeError:
        raise ValidationError("invalid JSON") from None
    return parse_document(data, now)


def parse_document(data, now):
    if not isinstance(data, dict):
        raise ValidationError("snapshot must be a JSON object")
    unknown = set(data) - DOC_REQUIRED - DOC_OPTIONAL
    if unknown:
        raise ValidationError("unknown snapshot field")
    missing = DOC_REQUIRED - set(data)
    if missing:
        raise ValidationError("missing snapshot field")
    if data["schema_version"] is not SCHEMA_VERSION:
        raise ValidationError("schema_version must be 1")
    if data["display_timezone"] != "America/New_York":
        raise ValidationError("display_timezone must be America/New_York")
    status = data["status"]
    if status not in DOC_STATUS:
        raise ValidationError("status is not an allowed snapshot state")
    collected = parse_time(data["collected_at"], "collected_at")
    if collected > now + FUTURE_SKEW:
        raise ValidationError("collected_at is in the future")
    attempt = parse_time(data["last_attempt_at"], "last_attempt_at") if "last_attempt_at" in data else collected
    if attempt > now + FUTURE_SKEW:
        raise ValidationError("last_attempt_at is in the future")
    if "last_success_at" in data:
        success = parse_time(data["last_success_at"], "last_success_at")
        if success > collected + dt.timedelta(seconds=2):
            raise ValidationError("last_success_at is after collected_at")
    elif status == "error":
        success = None
    else:
        success = collected
    if "query_end_time" in data and data["query_end_time"] is not None:
        query_end = parse_time(data["query_end_time"], "query_end_time")
        if query_end > collected + dt.timedelta(seconds=120):
            raise ValidationError("query_end_time is after collected_at")
        if query_end < collected - dt.timedelta(days=14):
            raise ValidationError("query_end_time is too far before collected_at")
    source_error = data.get("source_error")
    if status == "error":
        source_error = _plain(source_error, "source_error", 300, newlines=False)
    elif source_error not in (None, ""):
        raise ValidationError("source_error is only allowed on an error snapshot")
    else:
        source_error = None
    warnings = _warnings(data["warnings"])
    items = _items(data["items"], collected)
    if status == "empty" and items:
        raise ValidationError("an empty snapshot cannot contain posts")
    if status in {"ok", "starter_snapshot_not_live"} and not items:
        raise ValidationError("this snapshot state requires at least one post")
    teams = _teams(data.get("teams"), items, status)
    if status != "error" and success is None:
        success = collected
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": data["collected_at"].strip(),
        "last_attempt_at": data["last_attempt_at"].strip() if "last_attempt_at" in data else data["collected_at"].strip(),
        "last_success_at": None if success is None else (
            data["last_success_at"].strip() if "last_success_at" in data else data["collected_at"].strip()
        ),
        "display_timezone": "America/New_York",
        "status": status,
        "query_end_time": data.get("query_end_time"),
        "source_error": source_error,
        "warnings": warnings,
        "teams": teams,
        "items": items,
    }


def _warnings(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValidationError("warnings must be a short list")
    return [_plain(item, "warning", 400, newlines=False) for item in value]


def _plain(value, field, limit, newlines):
    if not isinstance(value, str):
        raise ValidationError(field + " must be plain text")
    if len(value) > limit or not value.strip():
        raise ValidationError(field + " must be plain text")
    for ch in value:
        if ch in "\n\t" and newlines:
            continue
        if ch in "<>" or ord(ch) < 32 or ord(ch) == 127:
            raise ValidationError(field + " must be plain text")
    return value


def _items(value, collected):
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValidationError("items must be a list of at most 75 posts")
    seen = set()
    cleaned = []
    for item in value:
        cleaned.append(_item(item, collected, seen))
    return cleaned


def _item(item, collected, seen):
    if not isinstance(item, dict):
        raise ValidationError("each post must be an object")
    if set(item) - ITEM_REQUIRED - ITEM_OPTIONAL or ITEM_REQUIRED - set(item):
        raise ValidationError("post fields do not match the contract")
    post_id = item["id"]
    if not isinstance(post_id, str) or not post_id.isdigit() or not 1 <= len(post_id) <= 20:
        raise ValidationError("post id must be the numeric status id")
    if post_id in seen:
        raise ValidationError("duplicate post id")
    seen.add(post_id)
    handle_key = item["author_handle"]
    if not isinstance(handle_key, str):
        raise ValidationError("author_handle is not allowlisted")
    folded = handle_key.casefold()
    if folded in REJECTED_HANDLES:
        raise ValidationError(REJECTED_HANDLES[folded])
    author = AUTHORS.get(folded)
    if author is None:
        raise ValidationError("author_handle is not allowlisted")
    if item["team"] != author["team"] or item["sport"] != TEAM_SPORT[author["team"]]:
        raise ValidationError("team and sport do not match the author")
    if item["source_provenance"] != author["role"]:
        raise ValidationError("source_provenance does not match the author")
    if item["status"] not in ITEM_STATUS:
        raise ValidationError("post status is not allowed")
    author_id = item["author_id"]
    if not isinstance(author_id, str) or not author_id.isdigit() or not 1 <= len(author_id) <= 20:
        raise ValidationError("author_id must be a numeric string")
    if author["author_id"] is not None and author_id != author["author_id"]:
        raise ValidationError("author_id does not match the allowlisted account")
    published = parse_time(item["published_at"], "published_at")
    item_collected = parse_time(item["collected_at"], "collected_at")
    if item_collected != collected:
        raise ValidationError("post collected_at must match the snapshot")
    if published > collected:
        raise ValidationError("published_at is after collected_at")
    url = _post_url(item["canonical_post_url"], author["handle"], post_id)
    if "evidence_files" in item:
        _evidence(item["evidence_files"])
    return {
        "id": post_id,
        "team": author["team"],
        "sport": TEAM_SPORT[author["team"]],
        "author_handle": author["handle"],
        "author_name": _plain(item["author_name"], "author_name", 80, newlines=False),
        "author_role": author["role"],
        "url": url,
        "published_at": item["published_at"].strip(),
        "collected_at": item["collected_at"].strip(),
        "summary": _plain(item["summary"], "summary", 500, newlines=False),
        "source_text": _plain(item["source_text"], "source_text", 4000, newlines=True),
    }


def _post_url(value, handle, post_id):
    if not isinstance(value, str) or len(value) > 180 or any(ch.isspace() for ch in value):
        raise ValidationError("canonical_post_url must be an https X post link")
    if "\\" in value or value.lower().startswith("javascript:"):
        raise ValidationError("canonical_post_url must be an https X post link")
    scheme, sep, rest = value.partition("://")
    if scheme != "https" or not sep:
        raise ValidationError("canonical_post_url must be an https X post link")
    if any(mark in rest for mark in ("?", "#", "@")):
        raise ValidationError("canonical_post_url must be an https X post link")
    host, slash, path = rest.partition("/")
    if host.lower() not in HOSTS or ":" in host or not slash:
        raise ValidationError("canonical_post_url must be an https X post link")
    parts = path.split("/")
    if len(parts) != 3 or parts[1] != "status" or parts[2] != post_id:
        raise ValidationError("canonical_post_url must be an https X post link")
    if parts[0].casefold() != handle.casefold():
        raise ValidationError("canonical_post_url must be an https X post link")
    return "https://" + host.lower() + "/" + handle + "/status/" + post_id


def _evidence(value):
    if not isinstance(value, list) or len(value) > 8:
        raise ValidationError("evidence_files is not a safe relative list")
    for entry in value:
        if not isinstance(entry, str) or len(entry) > 80:
            raise ValidationError("evidence_files is not a safe relative list")
        if not entry.startswith("raw/") or ".." in entry or "\\" in entry:
            raise ValidationError("evidence_files is not a safe relative list")
        leaf = entry[4:]
        if not leaf or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in leaf):
            raise ValidationError("evidence_files is not a safe relative list")


def _teams(raw, items, status):
    counts = {name: 0 for name in TEAM_ORDER}
    for item in items:
        counts[item["team"]] += 1
    if raw is None:
        if status == "partial":
            raise ValidationError("a partial snapshot must name the team error")
        return {
            name: {
                "status": "ok" if counts[name] else "empty",
                "detail": "" if counts[name] else "No posts in this snapshot.",
            }
            for name in TEAM_ORDER
        }
    if not isinstance(raw, dict):
        raise ValidationError("teams must be an object")
    if set(raw) - set(TEAM_ORDER):
        raise ValidationError("unknown team")
    if status == "partial" and not any(isinstance(v, dict) and v.get("status") == "error" for v in raw.values()):
        raise ValidationError("a partial snapshot must name the team error")
    out = {}
    for name in TEAM_ORDER:
        if name not in raw:
            out[name] = {
                "status": "ok" if counts[name] else "empty",
                "detail": "" if counts[name] else "No posts in this snapshot.",
            }
            continue
        entry = raw[name]
        if not isinstance(entry, dict) or set(entry) - {"status", "detail"} or "status" not in entry:
            raise ValidationError("team status is not allowed")
        if entry["status"] not in {"ok", "empty", "error"}:
            raise ValidationError("team status is not allowed")
        detail = entry.get("detail", "")
        if entry["status"] == "error":
            detail = _plain(detail, "team detail", 200, newlines=False)
        elif detail in ("", None):
            detail = "" if entry["status"] == "ok" else "No posts in this snapshot."
        else:
            detail = _plain(detail, "team detail", 200, newlines=False)
        out[name] = {"status": entry["status"], "detail": detail}
    return out


def view(doc, now, origin, inbox_error=None, source_error=None, attempt_at=None, freshness=None):
    success = parse_time(doc["last_success_at"], "last_success_at") if doc.get("last_success_at") else None
    collected = parse_time(doc["collected_at"], "collected_at") if doc.get("collected_at") else None
    stale = bool(success and (now - success).total_seconds() > 2 * planned_seconds(now))
    if freshness is None:
        if doc["status"] == "error" or inbox_error or source_error:
            freshness = "error"
        elif doc["status"] == "empty":
            freshness = "empty"
        elif stale:
            freshness = "stale"
        else:
            freshness = "fresh"
    if source_error is None:
        source_error = doc.get("source_error")
    if attempt_at is None:
        attempt_at = doc.get("last_attempt_at")
    published = [parse_time(item["published_at"], "published_at") for item in doc["items"]]
    newest = max(published) if published else None
    items = []
    for item in sorted(doc["items"], key=lambda row: parse_time(row["published_at"], "published_at"), reverse=True):
        published_at = parse_time(item["published_at"], "published_at")
        items.append({
            "id": item["id"],
            "team": item["team"],
            "sport": item["sport"],
            "author_handle": item["author_handle"],
            "author_name": item["author_name"],
            "author_role": item["author_role"],
            "role_label": "Official account" if item["author_role"] == "official" else "Reporter, attributed",
            "url": item["url"],
            "published_at": item["published_at"],
            "published_at_et": format_et(published_at),
            "summary": item["summary"],
            "source_text": item["source_text"],
            "older": (now - published_at).total_seconds() > OLDER_SECONDS,
        })
    teams = []
    for name in TEAM_ORDER:
        meta = doc["teams"][name]
        teams.append({
            "team": name,
            "sport": TEAM_SPORT[name],
            "status": meta["status"],
            "detail": meta["detail"],
            "count": sum(1 for item in items if item["team"] == name),
        })
    payload = {
        "ok": freshness != "error" or bool(items),
        "live_feed": False,
        "scheduled": False,
        "feed_kind": origin,
        "freshness": freshness,
        "stale": stale,
        "status": doc["status"],
        "collected_at": doc.get("collected_at"),
        "collected_at_et": format_et(collected) if collected else None,
        "last_attempt_at": attempt_at,
        "last_attempt_at_et": format_et(parse_time(attempt_at, "last_attempt_at")) if attempt_at else None,
        "last_success_at": doc.get("last_success_at"),
        "last_success_at_et": format_et(success) if success else None,
        "newest_published_at": newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None,
        "newest_published_at_et": format_et(newest) if newest else None,
        "page_checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_checked_at_et": format_et(now),
        "poll_seconds": 60,
        "cadence_label": CADENCE_LABEL,
        "poll_label": POLL_LABEL,
        "inbox_error": inbox_error,
        "source_error": source_error,
        "warnings": list(doc.get("warnings") or []),
        "teams": teams,
        "items": items,
    }
    payload["messages"] = _messages(payload)
    return payload


def _messages(payload):
    lines = []
    collected = payload.get("collected_at_et") or "an unknown time"
    if payload["feed_kind"] == "bundled_starter" or payload["status"] == "starter_snapshot_not_live":
        lines.append(
            f"Starter snapshot collected {collected}. Not a live feed. No refresh routine is scheduled."
        )
    if payload["inbox_error"] and payload["feed_kind"] == "bundled_starter":
        lines.append(
            "Inbox rejected (" + payload["inbox_error"] + "). Cards are the starter snapshot collected "
            + collected + ", not the rejected file."
        )
    elif payload["inbox_error"]:
        lines.append(
            "Inbox rejected (" + payload["inbox_error"] + "). Showing the last good snapshot. "
            "Collection time was not reset."
        )
    if payload["source_error"]:
        lines.append("Source error: " + payload["source_error"])
    if payload["freshness"] == "empty":
        lines.append("This collection succeeded and returned no posts.")
    if payload["stale"]:
        minutes = (2 * planned_seconds(parse_time(payload["page_checked_at"], "page_checked_at"))) // 60
        lines.append(
            f"Stale. Last successful collection is older than two proposed cycles ({minutes} minutes). "
            "Showing that snapshot."
        )
    elif payload["freshness"] == "fresh":
        lines.append("Last successful collection is inside two proposed cycles.")
    if payload["collected_at_et"] and payload["page_checked_at_et"]:
        lines.append(
            f"Updated {payload['collected_at_et']}. Page checked {payload['page_checked_at_et']}. "
            "This page check is not a new collection."
        )
    lines.append(payload["cadence_label"])
    lines.append(payload["poll_label"])
    lines.append(ATTRIBUTION)
    for warning in payload["warnings"]:
        lines.append("Note: " + warning)
    return lines


def empty_view(now, message):
    blank_teams = {name: {"status": "empty", "detail": "No posts in this snapshot."} for name in TEAM_ORDER}
    doc = {
        "schema_version": 1,
        "collected_at": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "display_timezone": "America/New_York",
        "status": "error",
        "source_error": message,
        "warnings": [],
        "teams": blank_teams,
        "items": [],
    }
    # view() parses collected_at. Build the error payload without a timestamp.
    payload = {
        "ok": False,
        "live_feed": False,
        "scheduled": False,
        "feed_kind": "error",
        "freshness": "error",
        "stale": False,
        "status": "error",
        "collected_at": None,
        "collected_at_et": None,
        "last_attempt_at": None,
        "last_attempt_at_et": None,
        "last_success_at": None,
        "last_success_at_et": None,
        "newest_published_at": None,
        "newest_published_at_et": None,
        "page_checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_checked_at_et": format_et(now),
        "poll_seconds": 60,
        "cadence_label": CADENCE_LABEL,
        "poll_label": POLL_LABEL,
        "inbox_error": message,
        "source_error": message,
        "warnings": [],
        "teams": [
            {"team": name, "sport": TEAM_SPORT[name], "status": "empty", "detail": "No posts in this snapshot.", "count": 0}
            for name in TEAM_ORDER
        ],
        "items": [],
    }
    payload["messages"] = [
        "Source error: " + message,
        f"Page checked {payload['page_checked_at_et']}. This page check is not a new collection.",
        CADENCE_LABEL,
        POLL_LABEL,
        ATTRIBUTION,
    ]
    return payload
