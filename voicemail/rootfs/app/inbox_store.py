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
        # Server-set. A caller cannot choose Drive ids, upload state, or source.
        "source": "",
        "drive_audio_id": "",
        "drive_transcript_id": "",
        "drive_status": "",
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
                "caller_masked": call_summary.caller_number(call_summary.strip_format(item.get("caller") or "")),
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
            }
        )
    unheard = sum(1 for item in items if not item.get("heard"))
    return {
        "state": str(len(items)),
        "attributes": {
            "friendly_name": "Inbound Voicemails",
            "icon": "mdi:voicemail",
            "voicemails": recent,
            "recent_items": recent,
            "unheard": unheard,
            "total": len(items),
            "source": "callcatch",
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
                if vid and str(item.get("voicemail_id") or "") == vid:
                    item["heard"] = True
                    found = True
                elif sid and str(item.get("short_id") or "") == sid:
                    item["heard"] = True
                    found = True
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

    def find_by_call(self, call_id: str) -> dict | None:
        safe = safe_id(call_id)
        if not safe:
            return None
        for item in self.list_items():
            if item.get("call_id") == safe or item.get("voicemail_id") == safe:
                return item
        return None
