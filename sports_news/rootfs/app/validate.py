"""Validate a personal sports snapshot. No network and no X client."""

import datetime as dt
import json
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
MAX_BYTES = 262144
MAX_ITEMS = 75
MAX_PER_TEAM = 75
OLDER_SECONDS = 72 * 3600
FUTURE_SKEW = dt.timedelta(minutes=5)
CADENCE_LABEL = (
    "Proposed collector, not scheduled: every 30 minutes from 7:00 AM to 11:00 PM ET, "
    "and hourly from 11:00 PM to 7:00 AM ET."
)
POLL_LABEL = "This page re-reads the saved snapshot every 60 seconds. That re-read is not a new collection and does not see an option changed while the app is running."
ACTIVATION_LABEL = (
    "A new snapshot is applied only when Sports News starts. Saving the option does not "
    "update this page until Sports News is restarted. The restart is only this app. "
    "The panel is unavailable during that restart. Supervisor's default stop wait is "
    "10 seconds, then this process starts and validates the option. That wait was not "
    "timed on a live Home Assistant restart."
)
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
# Numeric ids pinned from the Nov 8, 2026 public-profile responses.
# ZagMBB and BWagner_CapGaz ids are from that collector's raw profile payload.
AUTHORS = {
    "seahawks": {"handle": "Seahawks", "team": "Seattle Seahawks", "role": "official", "author_id": "23642374"},
    "navyfb": {"handle": "NavyFB", "team": "Navy Midshipmen", "role": "official", "author_id": "2429051735"},
    "zagmbb": {"handle": "ZagMBB", "team": "Gonzaga Bulldogs", "role": "official", "author_id": "602989093"},
    "theolawson_sr": {"handle": "TheoLawson_SR", "team": "Gonzaga Bulldogs", "role": "reporter", "author_id": "291955806"},
    "bwagner_capgaz": {"handle": "BWagner_CapGaz", "team": "Navy Midshipmen", "role": "reporter", "author_id": "872205181"},
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


class RollbackError(ValidationError):
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
    teams = _teams(data.get("teams"), items, status, data["collected_at"].strip())
    dated = [(meta["last_success_at"], parse_time(meta["last_success_at"], "last_success_at"))
             for meta in teams.values() if meta.get("last_success_at")]
    success_text = min(dated, key=lambda pair: pair[1])[0] if dated and status != "error" else None
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": data["collected_at"].strip(),
        "last_attempt_at": data["last_attempt_at"].strip() if "last_attempt_at" in data else data["collected_at"].strip(),
        "last_success_at": success_text,
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
    if not isinstance(value, list):
        raise ValidationError("items must be a list of at most 75 posts")
    seen = set()
    cleaned = []
    counts = {name: 0 for name in TEAM_ORDER}
    for item in value:
        cleaned.append(_item(item, collected, seen))
        team = cleaned[-1]["team"]
        counts[team] += 1
        if counts[team] > MAX_PER_TEAM:
            raise ValidationError("a team has more than 75 posts")
    if len(cleaned) > MAX_ITEMS:
        raise ValidationError("items must be a list of at most 75 posts")
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
    if author_id != author["author_id"]:
        raise ValidationError("author_id does not match the allowlisted account")
    published = parse_time(item["published_at"], "published_at")
    item_collected = parse_time(item["collected_at"], "collected_at")
    if item_collected > collected:
        raise ValidationError("post collected_at is after the snapshot")
    if published > item_collected:
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
        "author_id": author_id,
        "author_role": author["role"],
        "source_provenance": author["role"],
        "status": item["status"],
        "canonical_post_url": url,
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


def _team_success(entry, status_name, collected_text):
    if "last_success_at" in entry and entry["last_success_at"] not in (None, ""):
        parsed = parse_time(entry["last_success_at"], "last_success_at")
        if parsed > parse_time(collected_text, "collected_at") + dt.timedelta(seconds=2):
            raise ValidationError("team last_success_at is after collected_at")
        return entry["last_success_at"].strip()
    if status_name == "error":
        return None
    if status_name == "retain":
        return None
    return collected_text


def _teams(raw, items, status, collected_text):
    counts = {name: 0 for name in TEAM_ORDER}
    for item in items:
        counts[item["team"]] += 1
    if status == "empty":
        return {
            name: {"status": "empty", "detail": "No posts in this snapshot.", "last_success_at": collected_text}
            for name in TEAM_ORDER
        }
    if raw is None:
        if status == "partial":
            raise ValidationError("a partial snapshot must name the team error")
        return {
            name: _implicit_team(name, counts[name], collected_text)
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
            out[name] = _implicit_team(name, counts[name], collected_text)
            continue
        entry = raw[name]
        if not isinstance(entry, dict) or set(entry) - {"status", "detail", "last_success_at"} or "status" not in entry:
            raise ValidationError("team status is not allowed")
        if entry["status"] not in {"ok", "empty", "error"}:
            raise ValidationError("team status is not allowed")
        detail = entry.get("detail", "")
        if entry["status"] == "error":
            detail = _plain(detail, "team detail", 400, newlines=False)
        elif detail in ("", None):
            detail = "" if entry["status"] == "ok" else "No posts in this snapshot."
        else:
            detail = _plain(detail, "team detail", 400, newlines=False)
        out[name] = {
            "status": entry["status"],
            "detail": detail,
            "last_success_at": _team_success(entry, entry["status"], collected_text),
        }
    return out


def _implicit_team(name, count, collected_text):
    # Missing from the publisher file is not a successful empty. Merge keeps prior posts.
    if count:
        return {"status": "ok", "detail": "", "last_success_at": collected_text}
    return {"status": "retain", "detail": "", "last_success_at": None}


def apply_publication(previous, incoming):
    """Merge a validated publication onto the previous saved document.

    A team marked error keeps that team's previous posts and success time.
    A team omitted from the file is not treated as a successful empty.
    An older collected_at is rejected.
    """
    incoming_at = parse_time(incoming["collected_at"], "collected_at")
    if previous is not None:
        previous_at = parse_time(previous["collected_at"], "collected_at")
        if incoming_at < previous_at:
            raise RollbackError("older snapshot rejected")
    if incoming["status"] == "error":
        raise ValidationError("an error snapshot is not a success")
    items = []
    teams = {}
    any_error = False
    for name in TEAM_ORDER:
        inc = incoming["teams"][name]
        prev_meta = (previous or {}).get("teams", {}).get(name) or {}
        prev_items = [item for item in (previous or {}).get("items", []) if item["team"] == name]
        if inc["status"] == "error" or (inc["status"] == "retain" and prev_items):
            any_error = inc["status"] == "error" or any_error
            kept_success = prev_meta.get("last_success_at")
            if not kept_success and prev_items:
                kept_success = max(
                    prev_items,
                    key=lambda item: parse_time(item["collected_at"], "collected_at"),
                )["collected_at"]
            if inc["status"] == "error":
                detail = inc["detail"]
                if prev_items and kept_success:
                    detail = detail + " Previous posts kept. Success time was not reset."
                elif not prev_items:
                    detail = detail + " No previous posts for this team."
                teams[name] = {"status": "error", "detail": detail[:400], "last_success_at": kept_success}
                items.extend(prev_items)
            else:
                teams[name] = {
                    "status": prev_meta.get("status") or "ok",
                    "detail": prev_meta.get("detail") or "",
                    "last_success_at": kept_success,
                }
                items.extend(prev_items)
            continue
        if inc["status"] == "empty" or inc["status"] == "retain":
            teams[name] = {
                "status": "empty",
                "detail": "No posts in this snapshot.",
                "last_success_at": incoming["collected_at"],
            }
            continue
        teams[name] = {
            "status": "ok",
            "detail": "",
            "last_success_at": incoming["collected_at"],
        }
        items.extend(item for item in incoming["items"] if item["team"] == name)
    if any_error:
        status = "partial"
    elif not items:
        status = "empty"
    elif incoming["status"] == "starter_snapshot_not_live":
        status = "starter_snapshot_not_live"
    else:
        status = "ok"
    dated = [(meta["last_success_at"], parse_time(meta["last_success_at"], "last_success_at"))
             for meta in teams.values() if meta.get("last_success_at")]
    merged = {
        "schema_version": SCHEMA_VERSION,
        "collected_at": incoming["collected_at"],
        "last_attempt_at": incoming.get("last_attempt_at") or incoming["collected_at"],
        "last_success_at": min(dated, key=lambda pair: pair[1])[0] if dated else None,
        "display_timezone": "America/New_York",
        "status": status,
        "query_end_time": incoming.get("query_end_time"),
        "source_error": None,
        "warnings": list(incoming.get("warnings") or []),
        "teams": teams,
        "items": items,
    }
    return merged


def commit_candidate(previous, incoming, now):
    """Build the merged document and accept it only if the saved bytes still validate.

    Nothing is written here. Callers must keep the previous file when this raises.
    """
    merged = apply_publication(previous, incoming)
    raw = canonical_bytes(merged)
    if len(raw) > MAX_BYTES:
        raise ValidationError("merged snapshot is larger than 262144 bytes")
    checked = load_bytes(raw, now)
    return checked, raw


def canonical_bytes(doc):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "collected_at": doc["collected_at"],
        "display_timezone": "America/New_York",
        "status": doc["status"],
        "warnings": list(doc.get("warnings") or []),
        "items": [],
        "teams": {},
    }
    if doc.get("last_attempt_at"):
        payload["last_attempt_at"] = doc["last_attempt_at"]
    if doc.get("last_success_at"):
        payload["last_success_at"] = doc["last_success_at"]
    if doc.get("query_end_time"):
        payload["query_end_time"] = doc["query_end_time"]
    if doc.get("source_error"):
        payload["source_error"] = doc["source_error"]
    for item in sorted(doc["items"], key=lambda row: row["id"]):
        payload["items"].append({
            "id": item["id"],
            "team": item["team"],
            "sport": item["sport"],
            "canonical_post_url": item.get("canonical_post_url") or item["url"],
            "author_handle": item["author_handle"],
            "author_name": item["author_name"],
            "author_id": item["author_id"],
            "published_at": item["published_at"],
            "collected_at": item["collected_at"],
            "summary": item["summary"],
            "source_provenance": item.get("source_provenance") or item["author_role"],
            "source_text": item["source_text"],
            "status": item.get("status") or "fresh",
        })
    for name in TEAM_ORDER:
        meta = doc["teams"][name]
        entry = {"status": "empty" if meta["status"] == "retain" else meta["status"], "detail": meta.get("detail") or ""}
        if meta.get("last_success_at"):
            entry["last_success_at"] = meta["last_success_at"]
        if entry["status"] == "error" and not entry["detail"]:
            entry["detail"] = "Source error."
        payload["teams"][name] = entry
    if payload["status"] == "partial" and not any(entry["status"] == "error" for entry in payload["teams"].values()):
        payload["status"] = "ok" if payload["items"] else "empty"
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def view(doc, now, origin, inbox_error=None, source_error=None, attempt_at=None, freshness=None, startup_note=None):
    collected = parse_time(doc["collected_at"], "collected_at") if doc.get("collected_at") else None
    limit = 2 * planned_seconds(now)
    team_rows = []
    for name in TEAM_ORDER:
        meta = doc["teams"][name]
        success = parse_time(meta["last_success_at"], "last_success_at") if meta.get("last_success_at") else None
        team_rows.append({
            "team": name,
            "sport": TEAM_SPORT[name],
            "status": "empty" if meta["status"] == "retain" else meta["status"],
            "detail": meta.get("detail") or "",
            "last_success_at": meta.get("last_success_at"),
            "last_success_at_et": format_et(success) if success else None,
            "stale": bool(success and (now - success).total_seconds() > limit),
            "count": sum(1 for item in doc["items"] if item["team"] == name),
        })
    stale = any(row["stale"] for row in team_rows)
    team_error = any(row["status"] == "error" for row in team_rows)
    if freshness is None:
        if doc["status"] == "error" or team_error or inbox_error or source_error:
            freshness = "error"
        elif doc["status"] == "empty" or all(row["status"] == "empty" for row in team_rows):
            freshness = "empty"
        elif stale:
            freshness = "stale"
        else:
            freshness = "fresh"
    success = None
    dated = [row for row in team_rows if row.get("last_success_at")]
    if dated:
        oldest = min(dated, key=lambda row: parse_time(row["last_success_at"], "last_success_at"))
        success = parse_time(oldest["last_success_at"], "last_success_at")
        success_text = oldest["last_success_at"]
    else:
        success_text = None
    if source_error is None:
        source_error = doc.get("source_error")
    if not source_error and team_error:
        source_error = "; ".join(
            row["team"] + ": " + row["detail"] for row in team_rows if row["status"] == "error" and row["detail"]
        ) or "A team source failed."
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
    for row in team_rows:
        row["count"] = sum(1 for item in items if item["team"] == row["team"])
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
        "last_success_at": success_text,
        "last_success_at_et": format_et(success) if success else None,
        "newest_published_at": newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else None,
        "newest_published_at_et": format_et(newest) if newest else None,
        "page_checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_checked_at_et": format_et(now),
        "poll_seconds": 60,
        "cadence_label": CADENCE_LABEL,
        "poll_label": POLL_LABEL,
        "inbox_error": inbox_error,
        "startup_note": startup_note,
        "source_error": source_error,
        "warnings": list(doc.get("warnings") or []),
        "teams": team_rows,
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
    if any(row["status"] == "error" for row in payload["teams"]):
        lines.append("Failed teams still show their previous posts. Those success times were not reset.")
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
    if payload.get("startup_note"):
        lines.append(payload["startup_note"])
    lines.append(payload["cadence_label"])
    lines.append(payload["poll_label"])
    lines.append(ACTIVATION_LABEL)
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
