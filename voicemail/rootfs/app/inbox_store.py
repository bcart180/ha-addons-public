"""CallCatch inbox store.

JSON file under the add-on data directory. Writers take an exclusive lock,
then replace the file atomically (temp file, fsync, rename). Audio bytes live
beside the index, under Media or Share, never under /local.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import call_summary

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

STORE_VERSION = 1
RECENT_LIMIT = 20
SNIPPET_MAX = 180
AUDIO_EXTS = ("wav", "mp3", "m4a", "ogg")


def safe_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(value or ""))[:80]


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def pretty_phone(value: Any) -> str:
    raw = str(value or "").strip()
    d = digits(raw)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"+1 ({d[:3]}) {d[3:6]}-{d[6:]}"
    return raw


def tel_href(value: Any) -> str | None:
    d = digits(value)
    if len(d) == 10:
        d = "1" + d
    if len(d) < 11:
        return None
    return f"tel:+{d}"


def legacy_mode(setting: Any, legacy_file_exists: bool) -> bool:
    """auto follows the legacy file; on/off are explicit."""
    val = str(setting if setting is not None else "auto").strip().lower()
    if val in ("on", "true", "yes", "1"):
        return True
    if val in ("off", "false", "no", "0"):
        return False
    return bool(legacy_file_exists)


def decode_audio_b64(value: Any, limit: int) -> bytes:
    import base64

    if value is None or value == "":
        return b""
    if not isinstance(value, str):
        raise ValueError("bad-audio")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError("bad-audio") from exc
    if len(raw) > limit:
        raise ValueError("audio-too-large")
    return raw


def guess_ext(audio: bytes, hinted: str = "") -> str:
    hint = hinted.lower().lstrip(".")
    if hint in AUDIO_EXTS:
        return hint
    if audio.startswith(b"RIFF") and b"WAVE" in audio[:16]:
        return "wav"
    if audio.startswith(b"ID3") or audio[:2] == b"\xff\xfb":
        return "mp3"
    if audio.startswith(b"OggS"):
        return "ogg"
    if len(audio) > 8 and audio[4:8] == b"ftyp":
        return "m4a"
    return "wav"


def format_displays(iso: str) -> tuple[str, str]:
    text = (iso or "").strip()
    if not text:
        return "", ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    date_display = f"{parsed.month}/{parsed.day}/{parsed.year % 100:02d}"
    clock = parsed.strftime("%H:%M")
    timestamp = f"{parsed.strftime('%B')} {parsed.day}, {parsed.year} at {clock}"
    return date_display, timestamp


def transcript_text(body: dict) -> str:
    direct = body.get("transcript_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    raw = body.get("transcript")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        lines = []
        for turn in raw:
            if isinstance(turn, str) and turn.strip():
                lines.append(turn.strip())
            elif isinstance(turn, dict) and str(turn.get("text") or "").strip():
                role = str(turn.get("role") or "").lower()
                prefix = ""
                if role in ("user", "caller"):
                    prefix = "Caller: "
                elif role in ("assistant", "agent", "house"):
                    prefix = "Assistant: "
                lines.append(prefix + str(turn["text"]).strip())
        return "\n".join(lines).strip()
    return ""


_VERSTAT_RE = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
TRUST_VERDICTS = ("allow", "screen", "blocked")
CALLBACK_PENDING = "pending"
CALLBACK_TERMINAL = ("returned", "failed", "cancelled")
CALLBACK_STATUSES = (CALLBACK_PENDING, *CALLBACK_TERMINAL)
CALLBACK_KEYS = ("callback_status", "callback_requested_at", "callback_status_at", "callback_note")

# Follow-up extends callback_status. ``queued`` is the 0.5.7 pending request.
FOLLOWUP_STATES = (
    "new",
    "seen",
    "needs_callback",
    "fyi",
    "spam",
    "done",
    "queued",
    "draft_ready",
    "approved",
    "in_progress",
    "returned",
    "no_answer",
    "failed",
    "cancelled",
    "snoozed",
)
FOLLOWUP_QUEUE = ("queued", "draft_ready", "approved", "in_progress", "no_answer")
FOLLOWUP_ACTORS = ("bryson", "agent")
FOLLOWUP_KEYS = (
    "followup_state",
    "followup_due",
    "followup_prior",
    "draft_id",
    "call_ref",
    "outcome_note",
    "last_actor",
    "history",
)
HISTORY_MAX = 20
NO_ANSWER_REQUEUE_MAX = 2
_FOLLOWUP_EDGES = {
    "new": {"seen", "needs_callback", "fyi", "spam", "done"},
    "seen": {"needs_callback", "fyi", "spam", "done"},
    "needs_callback": {"queued", "fyi", "spam", "done"},
    "fyi": {"done", "needs_callback", "spam"},
    "spam": {"done"},
    "done": set(),
    "queued": {"draft_ready", "cancelled"},
    "draft_ready": {"approved", "cancelled", "queued"},
    "approved": {"in_progress", "cancelled"},
    "in_progress": {"returned", "no_answer", "failed", "cancelled"},
    "returned": set(),
    "no_answer": {"queued"},
    "failed": set(),
    "cancelled": {"needs_callback"},
    "snoozed": set(),
}
_UNSAFE_TEXT = re.compile(r"(https?:|javascript:|data:|file:|<script|</script|\$\{|`)", re.I)
_ISO_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_SAFE_REF = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")


def clean_verstat(value: Any) -> str:
    text = str(value or "").strip()
    return text if _VERSTAT_RE.fullmatch(text) else ""


def stir_summary(verstat: Any) -> dict:
    """Twilio StirVerstat -> {status, level}. Display signal only."""
    raw = clean_verstat(verstat)
    if not raw:
        return {"status": "absent", "level": ""}
    m = re.match(r"^TN-Validation-Passed-([ABC])(?:-|$)", raw, re.I)
    if m:
        return {"status": "passed", "level": m.group(1).upper()}
    m = re.match(r"^TN-Validation-Failed(?:-([ABC]))?(?:-|$)", raw, re.I)
    if m:
        return {"status": "failed", "level": (m.group(1) or "").upper()}
    return {"status": "none", "level": ""}


def item_from_payload(body: dict, now: datetime | None = None) -> dict:
    """Map an ingest body or a legacy row onto a store item. Drops secrets."""
    if not isinstance(body, dict):
        raise ValueError("bad-payload")
    call_id = safe_id(body.get("call_id") or body.get("CallSid") or "")
    vid = safe_id(body.get("voicemail_id") or body.get("message_id") or call_id)
    if not vid:
        raise ValueError("id-required")
    caller = str(body.get("caller") or body.get("from") or body.get("caller_pretty") or "").strip()
    pretty = str(body.get("caller_pretty") or body.get("caller_display") or "").strip() or pretty_phone(caller)
    title = str(body.get("caller_title") or pretty or "Unknown").strip() or "Unknown"
    text = transcript_text(body)
    if not text:
        text = str(body.get("snippet") or "").strip()
    when = now or datetime.now(timezone.utc)
    iso = str(body.get("date") or body.get("ended_at") or "").strip()
    if not iso:
        iso = when.isoformat()
    date_display, timestamp_display = format_displays(iso)
    if not date_display:
        date_display = str(body.get("date_display") or "")
        timestamp_display = str(body.get("timestamp_display") or date_display)
    try:
        duration = int(float(body.get("duration_sec") if body.get("duration_sec") not in (None, "") else body.get("duration_s") or 0))
    except (TypeError, ValueError):
        duration = 0
    if duration < 0:
        duration = 0
    snippet = str(body.get("snippet") or text.replace("\n", " "))[:SNIPPET_MAX]
    return {
        "voicemail_id": vid,
        "call_id": call_id or vid,
        "caller": caller,
        "caller_pretty": pretty,
        "caller_title": title,
        "caller_digits": digits(caller or pretty),
        "tel": tel_href(caller or pretty),
        "date": iso,
        "date_display": date_display,
        "timestamp_display": timestamp_display or date_display,
        "snippet": snippet,
        "transcript": text,
        "heard": bool(body.get("heard")),
        "duration_sec": duration,
        "duration_display": str(body.get("duration_display") or (f"{duration}s" if duration else "")),
        "has_audio": bool(body.get("has_audio")),
        "audio_ext": "",
        "audio_name": "",
        "audio_local": False,
        "missed_call": bool(body.get("missed_call")),
        "business": bool(body.get("business")),
        "short_id": str(body.get("short_id") or ""),
        "stir_verstat": clean_verstat(body.get("stir_verstat")),
        "trust_verdict": str(body.get("trust_verdict") or "") if str(body.get("trust_verdict") or "") in TRUST_VERDICTS else "",
        # 0.3.7 post-call summary: Voice-sent (validated) or derived locally.
        "summary": call_summary.summarize(body.get("summary"), text, caller or pretty),
        # Server-set. A caller cannot choose Drive ids, upload state, source,
        # or callback-request state (ingest cannot mark a call returned).
        "source": "",
        "drive_audio_id": "",
        "drive_transcript_id": "",
        "drive_status": "",
        "callback_status": "",
        "callback_requested_at": "",
        "callback_status_at": "",
        "callback_note": "",
        # Server-set. Ingest cannot choose a follow-up state or draft.
        "followup_state": "",
        "followup_due": "",
        "followup_prior": "",
        "draft_id": "",
        "call_ref": "",
        "outcome_note": "",
        "last_actor": "",
        "history": [],
    }


def callback_sensor_fields(item: dict) -> dict:
    """Status only. No callback note and no phone number."""
    status = str(item.get("callback_status") or "")
    if status not in CALLBACK_STATUSES:
        status = ""
    return {
        "callback_status": status,
        "callback_requested_at": str(item.get("callback_requested_at") or "") if status else "",
    }


def _blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or not _ISO_TIME.fullmatch(text):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _history(item: dict) -> list:
    raw = item.get("history")
    return [dict(entry) for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []


def _append_history(item: dict, frm: str, to: str, actor: str, extra: dict | None = None) -> None:
    entry = {"at": _now_iso(), "from": frm, "to": to, "actor": actor}
    if extra:
        entry.update(extra)
    history = _history(item)
    history.append(entry)
    item["history"] = history[-HISTORY_MAX:]


def default_followup(item: dict) -> str:
    """State for a row that has none stored yet. Does not dial."""
    status = str(item.get("callback_status") or "")
    if status == CALLBACK_PENDING:
        return "queued"
    if status in CALLBACK_TERMINAL:
        return status
    if str(item.get("trust_verdict") or "") == "blocked":
        return "spam"
    if call_summary.is_hangup(item):
        return "seen" if item.get("heard") else "new"
    transcript = item.get("transcript") or item.get("snippet") or ""
    caller = item.get("caller") or item.get("caller_pretty") or item.get("caller_digits") or ""
    if call_summary.asks_for_callback(transcript, caller):
        return "needs_callback"
    if item.get("heard"):
        return "seen"
    return "new"


def effective_followup(item: dict, now: datetime | None = None) -> str:
    """Stored state, or the default. An elapsed snooze returns to its prior state."""
    state = str(item.get("followup_state") or "")
    if state not in FOLLOWUP_STATES:
        return default_followup(item)
    if state != "snoozed":
        return state
    prior = str(item.get("followup_prior") or "")
    if prior not in FOLLOWUP_STATES or prior == "snoozed":
        prior = "seen"
    due = _parse_iso(item.get("followup_due"))
    clock = now or datetime.now(timezone.utc)
    if due is None or due <= clock:
        return prior
    return "snoozed"


def actionable_needs_callback(item: dict, now: datetime | None = None) -> bool:
    """Badge count: needs_callback, excluding hangup and spam."""
    if call_summary.alert_silent(item):
        return False
    return effective_followup(item, now) == "needs_callback"


def in_callback_queue(item: dict, now: datetime | None = None) -> bool:
    """Pending 0.5.7 requests and queued follow-ups. Snoozed rows stay out."""
    if effective_followup(item, now) == "snoozed":
        return False
    if str(item.get("callback_status") or "") == CALLBACK_PENDING:
        return True
    return effective_followup(item, now) == "queued"


def _allowed_number(item: dict, number: str) -> bool:
    proposed = digits(number)
    if not call_summary.valid_callback(proposed):
        return False
    pool = []
    summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
    for source in (
        item.get("caller"),
        item.get("caller_digits"),
        item.get("caller_pretty"),
        summary.get("callback"),
    ):
        found = digits(source)
        if call_summary.valid_callback(found):
            pool.append(found)
    transcript = call_summary.strip_format(item.get("transcript") or item.get("snippet") or "")
    pool.extend(call_summary._spoken_numbers(transcript))
    return any(call_summary._same_number(proposed, known) for known in pool)


def _verbatim_purpose(item: dict, purpose: str) -> bool:
    text = purpose.strip()
    if not text or len(text) > 160 or _UNSAFE_TEXT.search(text):
        return False
    hay = call_summary.strip_format(item.get("transcript") or item.get("snippet") or "").lower()
    return text.lower() in hay


def _approved_record(item: dict) -> dict | None:
    for entry in reversed(_history(item)):
        if entry.get("to") != "approved":
            continue
        if _allowed_number(item, str(entry.get("number") or "")) and _verbatim_purpose(item, str(entry.get("purpose") or "")):
            return entry
    return None


def masked_history(item: dict) -> list:
    """Audit trail for the admin panel. Numbers are last-4 only."""
    rows = []
    for entry in _history(item):
        row = {
            "at": str(entry.get("at") or ""),
            "from": str(entry.get("from") or ""),
            "to": str(entry.get("to") or ""),
            "actor": str(entry.get("actor") or ""),
        }
        if entry.get("number"):
            row["number_masked"] = call_summary.mask_number(entry.get("number"))
        if entry.get("purpose"):
            row["purpose"] = call_summary._clean(
                call_summary.mask_phone_runs(call_summary.plain_text(entry.get("purpose"))),
                160,
            )
        rows.append(row)
    return rows


def callback_queue_row(item: dict, now: datetime | None = None) -> dict:
    """Metadata for a queued callback. No transcript and no full number."""
    summary = call_summary.stored_or_extract(item)
    sensor_bits = call_summary.for_sensor(summary)
    facts = call_summary.card(item)
    medical = bool(facts.get("medical"))
    purpose = ""
    if not medical:
        purpose = call_summary._clean(
            call_summary.mask_phone_runs(facts.get("wants") or sensor_bits.get("summary_reason") or ""),
            120,
        )
    name = call_summary.mask_phone_runs(call_summary.plain_text(sensor_bits.get("summary_name") or "")).strip()[:80]
    if not name:
        label = call_summary.caller_label(item)
        if label and "•" not in label and any(ch.isalpha() for ch in label):
            name = label[:80]
    caller = item.get("caller") or item.get("caller_pretty") or ""
    status = str(item.get("callback_status") or "")
    if status not in CALLBACK_STATUSES:
        status = ""
    return {
        "voicemail_id": str(item.get("voicemail_id") or ""),
        "followup_state": effective_followup(item, now),
        "callback_status": status,
        "callback_requested_at": str(item.get("callback_requested_at") or "") if status == CALLBACK_PENDING else "",
        "from_masked": call_summary.mask_number(call_summary.strip_format(caller)),
        "display_name": name,
        "purpose": purpose,
        "urgency": sensor_bits.get("urgency") or "normal",
        "known_contact": call_summary.known_contact(item),
        "medical": medical,
        "check_transcript": medical,
        "alert_silent": call_summary.alert_silent(item),
        "quiet_hold": call_summary.quiet_hold(item, now),
    }


def followup_sensor_fields(item: dict) -> dict:
    """No note, draft, history, or number."""
    return {
        "followup_state": effective_followup(item),
        "known_contact": call_summary.known_contact(item),
        "alert_silent": call_summary.alert_silent(item),
    }


def sensor_state(items: list[dict]) -> dict:
    """Count in state, recent rows in attributes, for existing dashboards."""
    recent = []
    for item in items[:RECENT_LIMIT]:
        recent.append(
            {
                "voicemail_id": item.get("voicemail_id") or "",
                "call_id": item.get("call_id") or "",
                # Masked only: no raw caller/caller_pretty/caller_title/snippet/
                # transcript. Any HA user can read entity attributes; the admin
                # panel reads the store directly.
                "caller_masked": call_summary.mask_number(call_summary.strip_format(item.get("caller") or "")),
                "date": item.get("date") or "",
                "date_display": item.get("date_display") or "",
                "timestamp_display": item.get("timestamp_display") or "",
                "heard": bool(item.get("heard")),
                "has_audio": bool(item.get("has_audio")),
                "duration_sec": int(item.get("duration_sec") or 0),
                "duration_display": item.get("duration_display") or "",
                "missed_call": bool(item.get("missed_call")),
                "business": bool(item.get("business")),
                "short_id": item.get("short_id") or "",
                "stir_status": stir_summary(item.get("stir_verstat"))["status"],
                "trust_verdict": item.get("trust_verdict") or "",
                # Masked like the pyscript notifications (•••1234).
                **call_summary.for_sensor(call_summary.stored_or_extract(item)),
                "summary_fallback": call_summary.fallback_line(item),
                "caller_label_masked": call_summary.caller_label(item),
                **callback_sensor_fields(item),
                **followup_sensor_fields(item),
            }
        )
    unheard = sum(1 for item in items if not item.get("heard"))
    needs = sum(1 for item in items if actionable_needs_callback(item))
    return {
        "state": str(len(items)),
        "attributes": {
            "friendly_name": "Inbound Voicemails",
            "icon": "mdi:voicemail",
            "voicemails": recent,
            "recent_items": recent,
            "unheard": unheard,
            "needs_callback": needs,
            "total": len(items),
            "source": "callcatch",
            # Policy only. The live clock is computed when a queue is read
            # and in the notification blueprint, so a publish does not flap.
            "quiet": {
                "tz": call_summary.QUIET_TZ,
                "start": call_summary.QUIET_START,
                "end": call_summary.QUIET_END,
                "exception": call_summary.QUIET_EXCEPTION,
            },
        },
    }


def atomic_write_bytes(path: pathlib.Path, data: bytes, before_replace=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if before_replace is not None:
            before_replace(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


@contextmanager
def exclusive_lock(lock_path: pathlib.Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


class VoicemailStore:
    def __init__(self, path: pathlib.Path, audio_dir: pathlib.Path):
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self.audio_dir = audio_dir

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"version": STORE_VERSION, "items": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._quarantine()
            return {"version": STORE_VERSION, "items": {}}
        if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
            self._quarantine()
            return {"version": STORE_VERSION, "items": {}}
        return data

    def _quarantine(self) -> None:
        if not self.path.is_file():
            return
        dest = self.path.with_name(self.path.name + ".corrupt")
        try:
            os.replace(self.path, dest)
        except OSError:
            return

    def _write(self, data: dict) -> None:
        payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
        atomic_write_bytes(self.path, payload)

    def _audio_path(self, name: str) -> pathlib.Path | None:
        safe = pathlib.Path(name).name
        if not safe or safe != name or safe.startswith("."):
            return None
        path = (self.audio_dir / safe).resolve()
        root = self.audio_dir.resolve()
        if root != path and root not in path.parents:
            return None
        return path

    def list_items(self) -> list[dict]:
        with exclusive_lock(self.lock_path):
            items = [copy.deepcopy(v) for v in self._read()["items"].values() if isinstance(v, dict)]

        def sort_key(item: dict) -> str:
            return str(item.get("date") or "")

        items.sort(key=sort_key, reverse=True)
        return items

    def get(self, voicemail_id: str) -> dict | None:
        with exclusive_lock(self.lock_path):
            item = self._read()["items"].get(voicemail_id)
            return copy.deepcopy(item) if isinstance(item, dict) else None

    def upsert(self, item: dict, audio: bytes | None = None, ext: str = "wav", preserve_heard: bool = True) -> dict:
        vid = str(item.get("voicemail_id") or "")
        if not vid:
            raise ValueError("id-required")
        with exclusive_lock(self.lock_path):
            data = self._read()
            prev = data["items"].get(vid)
            if isinstance(prev, dict):
                if preserve_heard:
                    item["heard"] = bool(prev.get("heard")) or bool(item.get("heard"))
                for key in ("drive_audio_id", "drive_transcript_id"):
                    if not item.get(key) and prev.get(key):
                        item[key] = prev[key]
                if not item.get("drive_status") and prev.get("drive_status"):
                    item["drive_status"] = prev["drive_status"]
                for key in CALLBACK_KEYS:
                    if not str(item.get(key) or "") and prev.get(key):
                        item[key] = prev[key]
                for key in FOLLOWUP_KEYS:
                    if _blank(item.get(key)) and not _blank(prev.get(key)):
                        item[key] = prev[key]
                if audio is None and prev.get("audio_name") and not item.get("audio_name"):
                    item["audio_name"] = prev.get("audio_name") or ""
                    item["audio_ext"] = prev.get("audio_ext") or ""
                    item["audio_local"] = bool(prev.get("audio_local"))
                    item["has_audio"] = bool(prev.get("has_audio")) or bool(item.get("has_audio"))
            if audio is not None:
                name = f"{safe_id(vid) or 'audio'}.{ext}"
                dest = self._audio_path(name)
                if dest is None:
                    raise ValueError("bad-audio-name")
                atomic_write_bytes(dest, audio)
                item["audio_name"] = name
                item["audio_ext"] = ext
                item["audio_local"] = True
                item["has_audio"] = True
            data["items"][vid] = item
            self._write(data)
            return copy.deepcopy(item)

    def update_fields(self, voicemail_id: str, **fields: Any) -> dict | None:
        with exclusive_lock(self.lock_path):
            data = self._read()
            item = data["items"].get(voicemail_id)
            if not isinstance(item, dict):
                return None
            item.update(fields)
            data["items"][voicemail_id] = item
            self._write(data)
            return copy.deepcopy(item)

    def mark_heard(self, voicemail_id: str = "", short_id: str = "") -> bool:
        vid = voicemail_id.strip()
        sid = short_id.strip()
        if not vid and not sid:
            return False
        with exclusive_lock(self.lock_path):
            data = self._read()
            found = False
            for item in data["items"].values():
                if not isinstance(item, dict):
                    continue
                id_hit = bool(vid) and str(item.get("voicemail_id") or "") == vid
                sid_hit = bool(sid) and str(item.get("short_id") or "") == sid
                if not id_hit and not sid_hit:
                    continue
                prior = effective_followup(item)
                item["heard"] = True
                found = True
                if prior == "new":
                    _append_history(item, "new", "seen", "bryson")
                    item["followup_state"] = "seen"
                    item["last_actor"] = "bryson"
            if found:
                self._write(data)
            return found

    def clear_one(self, voicemail_id: str) -> dict | None:
        with exclusive_lock(self.lock_path):
            data = self._read()
            item = data["items"].pop(voicemail_id, None)
            if not isinstance(item, dict):
                return None
            self._unlink_audio(item)
            self._write(data)
            return copy.deepcopy(item)

    def clear_all(self) -> list[dict]:
        with exclusive_lock(self.lock_path):
            data = self._read()
            removed = [copy.deepcopy(v) for v in data["items"].values() if isinstance(v, dict)]
            for item in removed:
                self._unlink_audio(item)
            data["items"] = {}
            self._write(data)
            return removed

    def read_audio(self, item: dict) -> bytes | None:
        name = str(item.get("audio_name") or "")
        path = self._audio_path(name) if name else None
        if path is None or not path.is_file():
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def audio_file(self, item: dict) -> pathlib.Path | None:
        if not item.get("audio_local"):
            return None
        name = str(item.get("audio_name") or "")
        path = self._audio_path(name) if name else None
        if path is None or not path.is_file():
            return None
        return path

    def delete_local_audio(self, voicemail_id: str) -> None:
        with exclusive_lock(self.lock_path):
            data = self._read()
            item = data["items"].get(voicemail_id)
            if not isinstance(item, dict):
                return
            self._unlink_audio(item)
            item["audio_local"] = False
            item["has_audio"] = bool(item.get("drive_audio_id")) or bool(item.get("has_audio"))
            self._write(data)

    def _unlink_audio(self, item: dict) -> None:
        name = str(item.get("audio_name") or "")
        path = self._audio_path(name) if name else None
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def request_callback(self, voicemail_id: str) -> dict | None:
        """Record a pending callback request. Does not dial.

        None when the id is missing. ``created`` is false when a request is
        already pending (same timestamp, no rewrite).
        """
        with exclusive_lock(self.lock_path):
            data = self._read()
            item = data["items"].get(voicemail_id)
            if not isinstance(item, dict):
                return None
            if (
                str(item.get("callback_status") or "") == CALLBACK_PENDING
                and str(item.get("callback_requested_at") or "")
            ):
                return {"item": copy.deepcopy(item), "created": False}
            previous = effective_followup(item)
            now = _now_iso()
            item["callback_status"] = CALLBACK_PENDING
            item["callback_requested_at"] = now
            item["callback_status_at"] = ""
            item["callback_note"] = ""
            item["followup_state"] = "queued"
            item["followup_due"] = ""
            item["last_actor"] = "bryson"
            _append_history(item, previous, "queued", "bryson")
            data["items"][voicemail_id] = item
            self._write(data)
            return {"item": copy.deepcopy(item), "created": True}

    def set_callback_status(self, voicemail_id: str, status: str, note: str = "") -> dict | None:
        """Move a pending request to returned, failed, or cancelled.

        None when the id is missing. ``error`` is ``bad-status`` or
        ``not-pending``. The same status again is a no-op (``changed`` false).
        """
        if status not in CALLBACK_TERMINAL:
            return {"error": "bad-status"}
        with exclusive_lock(self.lock_path):
            data = self._read()
            item = data["items"].get(voicemail_id)
            if not isinstance(item, dict):
                return None
            current = str(item.get("callback_status") or "")
            if current == status:
                return {"item": copy.deepcopy(item), "changed": False}
            if current != CALLBACK_PENDING:
                return {"error": "not-pending", "item": copy.deepcopy(item)}
            previous = effective_followup(item)
            item["callback_status"] = status
            item["callback_status_at"] = _now_iso()
            item["callback_note"] = str(note or "")[:160]
            item["followup_state"] = status
            item["followup_due"] = ""
            item["outcome_note"] = str(note or "")[:280]
            item["last_actor"] = "bryson"
            _append_history(item, previous, status, "bryson")
            data["items"][voicemail_id] = item
            self._write(data)
            return {"item": copy.deepcopy(item), "changed": True}

    def transition_followup(
        self,
        voicemail_id: str,
        state: str,
        *,
        snooze_until: str = "",
        actor: str = "bryson",
        number: str = "",
        purpose: str = "",
        outcome_note: str = "",
        call_ref: str = "",
        draft_id: str = "",
    ) -> dict | None:
        """Allowlisted follow-up move. Does not dial or send.

        None when the id is missing. ``error`` is ``bad-*`` (400) or
        ``illegal`` / ``not-approved`` / ``max-attempts`` (409).
        ``fire_event`` is true only when this move newly sets callback pending.
        """
        if state not in FOLLOWUP_STATES:
            return {"error": "bad-state"}
        if actor not in FOLLOWUP_ACTORS:
            return {"error": "bad-actor"}
        for raw in (snooze_until, number, purpose, outcome_note, call_ref, draft_id):
            if raw and _UNSAFE_TEXT.search(str(raw)):
                return {"error": "bad-field"}
        if call_ref and not _SAFE_REF.fullmatch(call_ref):
            return {"error": "bad-ref"}
        if draft_id and not _SAFE_REF.fullmatch(draft_id):
            return {"error": "bad-ref"}
        if outcome_note and len(str(outcome_note)) > 280:
            return {"error": "bad-note"}
        with exclusive_lock(self.lock_path):
            data = self._read()
            item = data["items"].get(voicemail_id)
            if not isinstance(item, dict):
                return None
            current = effective_followup(item)
            stored_state = str(item.get("followup_state") or "")
            if state == "snoozed":
                due = _parse_iso(snooze_until)
                if due is None:
                    return {"error": "bad-snooze", "item": copy.deepcopy(item)}
                same = (
                    str(item.get("followup_state") or "") == "snoozed"
                    and str(item.get("followup_due") or "") == due.isoformat(timespec="milliseconds")
                )
                if same:
                    return {"item": copy.deepcopy(item), "changed": False, "fire_event": False}
                prior = current if current != "snoozed" else str(item.get("followup_prior") or "seen")
                if prior not in FOLLOWUP_STATES or prior == "snoozed":
                    prior = "seen"
                _append_history(item, current, "snoozed", actor)
                item["followup_prior"] = prior
                item["followup_state"] = "snoozed"
                item["followup_due"] = due.isoformat(timespec="milliseconds")
                item["last_actor"] = actor
                data["items"][voicemail_id] = item
                self._write(data)
                return {"item": copy.deepcopy(item), "changed": True, "fire_event": False}
            if state == current and stored_state == current:
                return {"item": copy.deepcopy(item), "changed": False, "fire_event": False}
            waking = stored_state == "snoozed" and state == str(item.get("followup_prior") or "")
            if stored_state == "snoozed" and not waking:
                return {"error": "illegal", "item": copy.deepcopy(item)}
            if not waking and state not in _FOLLOWUP_EDGES.get(current, ()):
                return {"error": "illegal", "item": copy.deepcopy(item)}
            if current == "no_answer" and state == "queued":
                requeues = sum(
                    1 for entry in _history(item) if entry.get("from") == "no_answer" and entry.get("to") == "queued"
                )
                if requeues >= NO_ANSWER_REQUEUE_MAX:
                    return {"error": "max-attempts", "item": copy.deepcopy(item)}
            extra: dict[str, str] = {}
            if state == "approved":
                if not _allowed_number(item, number):
                    return {"error": "bad-number", "item": copy.deepcopy(item)}
                if not _verbatim_purpose(item, purpose):
                    return {"error": "bad-purpose", "item": copy.deepcopy(item)}
                extra = {"number": digits(number), "purpose": purpose.strip()[:160]}
            if state == "in_progress":
                record = _approved_record(item)
                if record is None or (not waking and current != "approved"):
                    return {"error": "not-approved", "item": copy.deepcopy(item)}
                if number and not call_summary._same_number(digits(number), str(record.get("number") or "")):
                    return {"error": "bad-number", "item": copy.deepcopy(item)}
            if state in ("returned", "no_answer", "failed") and outcome_note:
                if _UNSAFE_TEXT.search(outcome_note):
                    return {"error": "bad-note", "item": copy.deepcopy(item)}
            was_pending = str(item.get("callback_status") or "") == CALLBACK_PENDING
            fire = False
            now = _now_iso()
            if state in FOLLOWUP_QUEUE:
                if not was_pending:
                    item["callback_status"] = CALLBACK_PENDING
                    item["callback_requested_at"] = now
                    item["callback_status_at"] = ""
                    item["callback_note"] = ""
                    fire = state == "queued"
            elif state in CALLBACK_TERMINAL:
                item["callback_status"] = state
                item["callback_status_at"] = now
                if outcome_note:
                    item["callback_note"] = str(outcome_note)[:160]
            elif str(item.get("callback_status") or "") == CALLBACK_PENDING:
                item["callback_status"] = ""
                item["callback_requested_at"] = ""
                item["callback_status_at"] = ""
                item["callback_note"] = ""
            if state == "draft_ready":
                item["draft_id"] = draft_id or str(item.get("draft_id") or "") or f"d{uuid.uuid4().hex[:16]}"
            if call_ref:
                item["call_ref"] = call_ref
            if outcome_note and state in ("returned", "no_answer", "failed", "cancelled"):
                item["outcome_note"] = str(outcome_note)[:280]
            item["followup_state"] = state
            item["followup_due"] = ""
            item["last_actor"] = actor
            origin = stored_state if stored_state in FOLLOWUP_STATES else current
            _append_history(item, origin, state, actor, extra or None)
            data["items"][voicemail_id] = item
            self._write(data)
            return {"item": copy.deepcopy(item), "changed": True, "fire_event": fire}

    def find_by_call(self, call_id: str) -> dict | None:
        safe = safe_id(call_id)
        if not safe:
            return None
        for item in self.list_items():
            if item.get("call_id") == safe or item.get("voicemail_id") == safe:
                return item
        return None
