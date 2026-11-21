"""Telnyx carrier recording -> private file -> xAI transcript (CallCatch 0.8.0, allowlist 0.8.1, download retries 0.8.2).

Telnyx records the bridged call from answer (dual channel).  This module never
places or answers a call.  Two inputs land on one job:

* a signed webhook relay (call lifecycle + ``call.recording.saved``)
* a poll of ``GET /v2/recordings`` (missed webhook, or a restart mid-call)

Only recordings and events from an allowlisted Telnyx connection are
touched (0.8.1).  An empty allowlist touches nothing: no job, no download, no
transcript, no email, no remote delete.  Another connection on the same
account (for example an outbound-only TeXML app) is never read or deleted.

Jobs dedupe by recording id, then call session id, then leg id, then call
control id.  The remote recording is deleted only after the local file and
the transcript have been re-read from disk.  A failure stays on the job
(``dead`` or a retry); it is not dropped.

Nothing here logs a URL, a key, a transcript, or a full number.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import pathlib
import re
import socket
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import unquote, urlsplit

from inbox_store import atomic_write_bytes, exclusive_lock

log = logging.getLogger("voicemail.capture")

VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# Telnyx connection ids are decimal strings.  Anything else fails the whole allowlist closed.
CONNECTION_RE = re.compile(r"^[0-9]{1,32}$")
MAX_CONNECTIONS = 10
SKIP_ERROR = "connection-not-allowed"
FILE_RE = re.compile(r"^[a-f0-9]{32}\.(wav|mp3)$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$")
# Telnyx documents this bucket in "Storing call recordings":
# https://s3.amazonaws.com/telephony-recorder-prod/<key>
TELNYX_RECORDING_BUCKET = "telephony-recorder-prod"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
MAX_POLL_PAGES = 10
DISCARD_LIMIT = 4096
EVENTS = (
    "call.initiated",
    "call.answered",
    "call.hangup",
    "call.recording.saved",
    "call.recording.error",
)
EXTS = ("wav", "mp3")
URL_TTL_S = 540
GIVE_UP_S = 6 * 3600
MAX_TRIES = 8
MAX_AUDIO = 32 * 1024 * 1024
MAX_JOBS = 500
LOOKBACK_S = 6 * 3600
POLL_OVERLAP_S = 120
LEASE_S = 90
TRANSCRIPT_MAX = 60_000
# bad-type is retried (0.8.2) on its own counter: three backoffs
# (about 5 s, then 15 s, then 45 s), then terminal on the next failure.
BAD_TYPE_TRIES = 3
BAD_TYPE_DELAYS = (5, 15, 45)
TERMINAL_DOWNLOAD = frozenset({"oversize", "ssrf"})
HARD_REJECT_TYPES = frozenset({"text/html", "application/json", "application/xml"})
_STATE_RANK = {"wait": 0, "download": 1, "transcribe": 2, "attach": 3, "delete": 4, "done": 5, "dead": 5,
               "skipped": 5}
CLOSED = frozenset({"done", "dead", "skipped"})


def parse_connection_ids(raw: Any) -> frozenset[str]:
    """Comma or space separated Telnyx connection ids.

    Fail closed: blank, more than MAX_CONNECTIONS, or any token that is not a
    decimal id gives an empty allowlist, and an empty allowlist touches nothing.
    """
    tokens = [t for t in re.split(r"[\s,]+", str(raw or "").strip()) if t]
    if not tokens or len(tokens) > MAX_CONNECTIONS:
        return frozenset()
    if any(not CONNECTION_RE.fullmatch(t) for t in tokens):
        return frozenset()
    return frozenset(tokens)


def connection_id_of(value: Any) -> str:
    """The connection id as Telnyx sends it (string or integer), or "" when it is not a decimal id."""
    if isinstance(value, bool):
        return ""
    text = str(value if value is not None else "").strip()
    return text if CONNECTION_RE.fullmatch(text) else ""


def now_ts() -> float:
    return time.time()


def last4(num: str) -> str:
    digits = re.sub(r"\D", "", num or "")
    return ("•••" + digits[-4:]) if len(digits) >= 4 else "none"


def address_is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """False for CGNAT (100.64.0.0/10) and every other non-global range."""
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return False
    if (ip.is_multicast or ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_unspecified):
        return False
    return bool(ip.is_global)


def _path_traverses(path: str) -> bool:
    """True when a path segment is '..', including percent-encoded forms."""
    raw = path or ""
    for candidate in (raw, unquote(raw), unquote(unquote(raw))):
        if any(segment == ".." for segment in candidate.split("/")):
            return True
    return False


def url_allowed(url: str) -> bool:
    """https only, no userinfo, port 443.

    Hosts are Telnyx's own names, or the one S3 bucket Telnyx documents for
    call recordings (path-style or virtual-hosted).  IP literals are rejected.
    """
    if not isinstance(url, str) or not url or len(url) > 4096:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    if parts.port not in (None, 443):
        return False
    if ".." in host or host.startswith(".") or any(not label for label in host.split(".")):
        return False
    if _path_traverses(parts.path or ""):
        return False
    try:
        # A numeric host is never a recording CDN.  Reject before any DNS.
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if host.endswith(".telnyx.com") and host != "telnyx.com":
        return True
    bucket = TELNYX_RECORDING_BUCKET
    path = parts.path or ""
    if host == "s3.amazonaws.com":
        return path == f"/{bucket}" or path.startswith(f"/{bucket}/")
    if host == f"{bucket}.s3.amazonaws.com":
        return True
    prefix = f"{bucket}.s3."
    if host.startswith(prefix) and host.endswith(".amazonaws.com"):
        region = host[len(prefix):-len(".amazonaws.com")]
        return bool(region) and bool(re.fullmatch(r"[a-z0-9-]+", region))
    return False


def host_is_public(host: str) -> bool | None:
    """True when every address is public.  False when one is private.  None if DNS failed (retry)."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if not infos:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not address_is_public(ip):
            return False
    return True


async def _discard(resp: Any, limit: int = DISCARD_LIMIT) -> None:
    """Drop an error or unwanted body without reading it all.  Cap, then release."""
    try:
        await resp.content.read(limit)
    except Exception:  # noqa: BLE001
        pass
    resp.release()


def magic_ok(blob: bytes) -> bool:
    if len(blob) < 12:
        return False
    if blob[:4] == b"RIFF" and blob[8:12] == b"WAVE":
        return True
    if blob[:3] == b"ID3":
        return True
    return blob[0] == 0xFF and (blob[1] & 0xE0) == 0xE0


def content_type_base(value: str) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def content_type_hard_reject(value: str) -> bool:
    """HTML, JSON, XML, any text/*, or a comma-joined type is never accepted, even if the bytes look like audio."""
    raw = str(value or "")
    if "," in raw:
        return True
    base = content_type_base(raw)
    if base in HARD_REJECT_TYPES:
        return True
    if base.startswith("text/"):
        return True
    if base.endswith(("+json", "+xml")):
        return True
    if base == "application/xhtml+xml":
        return True
    return False


def content_type_for_log(value: str) -> str:
    """A short type token for logs.  Never a URL or a body."""
    text = content_type_base(value).replace("\n", " ").replace("\r", " ")
    return (text[:80] or "-")


def log_fetch_decision(error: str, check: str, http_status: int, content_type: str, n_bytes: int) -> None:
    """Log why a download was rejected.  No body bytes and no URL."""
    log.info(
        "recording fetch %s check=%s http=%s type=%s bytes=%s",
        error, check or "-", int(http_status), content_type_for_log(content_type), int(n_bytes),
    )


def classify_audio(content_type: str, blob: bytes) -> tuple[str, str]:
    """Return (error, check).  Empty error means the bytes are acceptable audio.

    A valid RIFF/WAVE, ID3, or MPEG frame is accepted even when the Content-Type
    is unexpected, unless the type is a hard reject (check=header): text/html,
    application/json, application/xml, application/xhtml+xml, any text/* type,
    any +json or +xml suffix, or a comma-joined type.  Wrong type and wrong
    magic is still a failure (check=magic).
    """
    if content_type_hard_reject(content_type):
        return "bad-type", "header"
    if not magic_ok(blob):
        return "bad-type", "magic"
    return "", ""


def _iso(value: Any) -> str:
    text = str(value or "").strip()
    return text if ISO_RE.fullmatch(text) else ""


def _epoch(value: Any) -> float:
    text = _iso(value)
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        return 0.0
    return dt.timestamp()


def _id(value: Any) -> str:
    text = str(value or "").strip()
    return text if ID_RE.fullmatch(text) else ""


def _caller(value: Any) -> str:
    raw = re.sub(r"[\s()\-.]", "", str(value or ""))[:20]
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if 11 <= len(digits) <= 15:
        return "+" + digits
    return ""


def _fmt_from_urls(urls: Any) -> tuple[str, str]:
    if not isinstance(urls, dict):
        return "", ""
    for fmt in EXTS:
        url = urls.get(fmt)
        if isinstance(url, str) and url.startswith("https://") and len(url) <= 4096:
            return fmt, url
    return "", ""


def duration_seconds(start: float, end: float, millis: Any = None, stt_seconds: Any = None) -> int:
    if isinstance(millis, (int, float)) and not isinstance(millis, bool) and millis >= 0:
        return int(round(float(millis) / 1000.0))
    if start and end and end >= start:
        return int(round(end - start))
    if isinstance(stt_seconds, (int, float)) and not isinstance(stt_seconds, bool) and stt_seconds >= 0:
        return int(round(float(stt_seconds)))
    return 0


def empty_notice(seconds: int) -> str:
    n = max(0, int(seconds))
    return f"caller hung up after {n} seconds, no speech detected"


# Assistant-opening cues used to map Telnyx dual-channel legs to roles.
# Channel index is not enough: Telnyx puts the first call leg on channel A,
# and on a bridged SIP call that leg is not always the inbound caller.
# The phrase list is English-only.
_ASSISTANT_OPENING = re.compile(
    r"you(?:['\u2019]ve| have) reached"
    r"|\brecorded line"
    r"|how may i\b"
    r"|this (?:call|line) is recorded",
    re.IGNORECASE,
)
_OPENING_WORDS = 15
_LEADING_ROLE = re.compile(
    r"^(\s*)(house|assistant|agent|caller)\s*:",
    re.IGNORECASE,
)


def _explicit_index(ch: dict) -> int | None:
    """Integer channel index, or None when missing, bool, or non-int."""
    if "index" not in ch:
        return None
    raw = ch["index"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _channel_words(ch: dict) -> list[tuple[float, str]]:
    words: list[tuple[float, str]] = []
    for word in ch.get("words") or []:
        if not isinstance(word, dict):
            continue
        token = str(word.get("text") or "").strip()
        if not token:
            continue
        try:
            start = float(word.get("start") or 0)
        except (TypeError, ValueError):
            start = 0.0
        words.append((start, token))
    return words


def _detection_tokens(ch: dict) -> list[str]:
    """Same tokens used for labels: words when present, otherwise the text field."""
    words = _channel_words(ch)
    if words:
        return [token for _start, token in words]
    text = str(ch.get("text") or "").strip()
    return text.split() if text else []


def _first_word_start(ch: dict) -> float | None:
    words = _channel_words(ch)
    if not words:
        return None
    return min(start for start, _token in words)


def _opening_match(tokens: list[str]) -> bool:
    if not tokens:
        return False
    return bool(_ASSISTANT_OPENING.search(" ".join(tokens[:_OPENING_WORDS])))


def _any_match(tokens: list[str]) -> bool:
    return bool(tokens) and bool(_ASSISTANT_OPENING.search(" ".join(tokens)))


def _assistant_role_index(chans: list) -> int | None:
    """Assistant channel index when the dual-channel role rule is fully met.

    Returns None (unlabeled) unless every check holds: exactly two dict
    channels with explicit, distinct integer indexes; the English
    assistant-opening match is in that channel's first ~15 words; that
    channel speaks first when both have word timings; and the other
    channel does not match anywhere.
    """
    dicts = [ch for ch in chans if isinstance(ch, dict)]
    if len(dicts) != 2:
        return None
    i0 = _explicit_index(dicts[0])
    i1 = _explicit_index(dicts[1])
    if i0 is None or i1 is None or i0 == i1:
        return None
    tokens0 = _detection_tokens(dicts[0])
    tokens1 = _detection_tokens(dicts[1])
    if _opening_match(tokens0) and not _any_match(tokens1):
        assistant, other, idx = dicts[0], dicts[1], i0
    elif _opening_match(tokens1) and not _any_match(tokens0):
        assistant, other, idx = dicts[1], dicts[0], i1
    else:
        return None
    a_start = _first_word_start(assistant)
    o_start = _first_word_start(other)
    if a_start is not None and o_start is not None and a_start > o_start:
        return None
    return idx


def _neutralize_leading_roles(text: str) -> str:
    """Stop a literal leading role word plus colon from looking like a label."""
    lines: list[str] = []
    for line in text.split("\n"):
        m = _LEADING_ROLE.match(line)
        if not m:
            lines.append(line)
            continue
        rest = line[m.end():].lstrip()
        prefix = f"{m.group(1)}{m.group(2)}"
        lines.append(f"{prefix} {rest}" if rest else prefix)
    return "\n".join(lines)


def _unlabeled_transcript(result: dict, chans: list) -> str:
    """Merged text with no speaker prefix, rather than a guessed label."""
    text = str(result.get("text") or "").strip()
    if not text:
        words: list[tuple[float, str]] = []
        for ch in chans:
            if not isinstance(ch, dict):
                continue
            words.extend(_channel_words(ch))
        if words:
            words.sort()
            text = " ".join(token for _start, token in words)
        else:
            parts = []
            for ch in chans:
                if not isinstance(ch, dict):
                    continue
                tokens = _detection_tokens(ch)
                if tokens:
                    parts.append(" ".join(tokens))
            text = "\n".join(parts)
    return _neutralize_leading_roles(text)[:TRANSCRIPT_MAX]


def turns_from_stt(result: dict, channels: str) -> str:
    """Caller / Assistant lines when dual-channel roles can be assigned.

    Telnyx dual-channel audio puts the first call leg on channel A (index 0)
    and the remaining legs on channel B.  Which leg is the inbound caller
    depends on how the bridge is recorded, so index 0 is not assumed to be
    the caller.  Roles are assigned only when `_assistant_role_index`
    returns an index; otherwise the unlabeled merged transcript is
    returned rather than a wrong label.
    """
    unlabeled = _neutralize_leading_roles(
        str(result.get("text") or "").strip()
    )[:TRANSCRIPT_MAX]
    if str(channels).lower() != "dual":
        return unlabeled
    chans = result.get("channels") if isinstance(result.get("channels"), list) else []
    if not chans:
        return unlabeled
    assistant_idx = _assistant_role_index(chans)
    if assistant_idx is None:
        return _unlabeled_transcript(result, chans)

    def role_for(idx: int) -> str:
        return "Assistant" if idx == assistant_idx else "Caller"

    words: list[tuple[float, int, str]] = []
    for ch in chans:
        if not isinstance(ch, dict):
            continue
        idx = _explicit_index(ch)
        if idx is None:
            return _unlabeled_transcript(result, chans)
        for start, token in _channel_words(ch):
            words.append((start, idx, token))
    if words:
        words.sort()
        lines: list[dict] = []
        for _start, idx, token in words:
            role = role_for(idx)
            if lines and lines[-1]["role"] == role:
                lines[-1]["text"] += " " + token
            else:
                lines.append({"role": role, "text": token})
        return "\n".join(f"{ln['role']}: {ln['text']}" for ln in lines)[:TRANSCRIPT_MAX]
    texts = []
    for ch in chans:
        if not isinstance(ch, dict):
            continue
        text = str(ch.get("text") or "").strip()
        if text:
            idx = _explicit_index(ch)
            if idx is None:
                return _unlabeled_transcript(result, chans)
            texts.append((idx, f"{role_for(idx)}: {text}"))
    texts.sort()
    if texts:
        return "\n".join(t for _i, t in texts)[:TRANSCRIPT_MAX]
    return unlabeled


def interpret_fetch(result: Any, *, content_type: str = "", http_status: int = 0) -> tuple[str, bytes]:
    """Return (error, body).  error is empty when the bytes are acceptable audio."""
    if isinstance(result, dict):
        err = str(result.get("error") or "download")
        if err not in {"oversize", "bad-type", "ssrf", "redirect", "download", "empty"}:
            err = "download"
        if err == "bad-type":
            check = str(result.get("check") or "magic")
            status = int(result.get("http_status") or http_status or 0)
            n_bytes = int(result.get("bytes") or 0)
            ctype = str(result.get("content_type") or content_type or "")
            log_fetch_decision(err, check, status, ctype, n_bytes)
        return err, b""
    if not isinstance(result, (bytes, bytearray)) or not result:
        return "empty" if result == b"" else "download", b""
    blob = bytes(result)
    if len(blob) > MAX_AUDIO:
        return "oversize", b""
    err, check = classify_audio(content_type, blob)
    if err:
        log_fetch_decision(err, check, http_status, content_type, len(blob))
        return err, b""
    return "", blob


def file_stem(recording_id: str) -> str:
    return hashlib.sha256(f"telnyx:{recording_id}".encode()).hexdigest()[:32]


def _blank_job(jid: str, now: float) -> dict:
    return {
        "id": jid,
        "state": "wait",
        "created_at": now,
        "updated_at": now,
        "event_ids": [],
        "call_session_id": "",
        "call_leg_id": "",
        "call_control_id": "",
        "recording_id": "",
        "connection_id": "",
        "from": "",
        "start_ts": 0.0,
        "end_ts": 0.0,
        "duration_s": 0,
        "channels": "single",
        "format": "wav",
        "url": "",
        "url_at": 0.0,
        "url_rejected": False,
        "file": "",
        "bytes": 0,
        "sha256": "",
        "transcript": "",
        "empty_speech": False,
        "ref": "",
        "deleted": "",
        "tries": 0,
        "bad_type_tries": 0,
        "next_at": 0.0,
        "last_error": "",
        "lease": 0,
        "lease_until": 0.0,
        "done_at": 0.0,
        "hangup": False,
    }


class Capture:
    def __init__(self, data_dir: pathlib.Path, retention_days: int = 30,
                 allowed_connections: Any = frozenset()):
        # Jobs live next to the audio, inside the backup-excluded folder.
        self.dir = data_dir / "recordings"
        self.path = self.dir / "jobs.json"
        self.lock_path = self.dir / "jobs.lock"
        self.retention_s = max(1, int(retention_days)) * 86400
        allowed = frozenset(str(c) for c in (allowed_connections or ()))
        # One bad id empties the list (fail closed), the same rule as the option parser.
        self.allowed = allowed if allowed and all(CONNECTION_RE.fullmatch(c) for c in allowed) else frozenset()
        self.skipped = 0

    def connection_allowed(self, value: Any) -> bool:
        """True only for a non-empty allowlist that names this connection id."""
        cid = connection_id_of(value)
        return bool(self.allowed) and bool(cid) and cid in self.allowed

    def _skip_input(self, kind: str) -> dict:
        """A recording or event from another connection.  Nothing is stored or fetched."""
        self.skipped += 1
        log.info("recording skipped %s connection not allowed", kind)
        return {"ok": True, "ignored": True, "duplicate": False, "id": "", "reason": "connection"}

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": VERSION, "watermark": 0, "jobs": {}}
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
            return {"version": VERSION, "watermark": 0, "jobs": {}}
        data.setdefault("watermark", 0)
        data.setdefault("version", VERSION)
        return data

    def _write(self, data: dict) -> None:
        """Private dir and files before any audio exists.  mkdir's mode is masked by umask, so chmod too."""
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        if not self.lock_path.exists():
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        if (self.lock_path.stat().st_mode & 0o777) != 0o600:
            os.chmod(self.lock_path, 0o600)

        def _private(tmp: pathlib.Path) -> None:
            os.chmod(tmp, 0o600)

        atomic_write_bytes(self.path, json.dumps(data, sort_keys=True).encode("utf-8"), before_replace=_private)

    def counts(self) -> dict:
        out: dict[str, int] = {}
        for job in self.jobs().values():
            st = str(job.get("state") or "")
            out[st] = out.get(st, 0) + 1
            err = str(job.get("last_error") or "")
            if job.get("state") == "dead" and err:
                out["dead:" + err] = out.get("dead:" + err, 0) + 1
        return out

    def jobs(self) -> dict:
        with exclusive_lock(self.lock_path):
            return json.loads(json.dumps(self._read()["jobs"]))

    def get(self, jid: str) -> dict | None:
        job = self.jobs().get(jid)
        return job if isinstance(job, dict) else None

    def _find(self, jobs: dict, *, recording_id: str = "", session: str = "", leg: str = "", control: str = "") -> str:
        if recording_id:
            hit = jobs.get(recording_id)
            if isinstance(hit, dict) and hit.get("recording_id") == recording_id:
                return recording_id
        for key, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if recording_id and job.get("recording_id") == recording_id:
                return key
        for key, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if session and job.get("call_session_id") == session and self._recording_compatible(job, recording_id):
                return key
        for key, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if leg and job.get("call_leg_id") == leg and self._recording_compatible(job, recording_id):
                return key
            if control and job.get("call_control_id") == control and self._recording_compatible(job, recording_id):
                return key
        return ""

    @staticmethod
    def _recording_compatible(job: dict, recording_id: str) -> bool:
        """A second recording of the same call is its own job."""
        existing = str(job.get("recording_id") or "")
        if recording_id and existing and existing != recording_id:
            return False
        return True

    def _rekey(self, jobs: dict, old: str, recording_id: str) -> str:
        if not recording_id or old == recording_id:
            return old
        job = jobs.get(old)
        if not isinstance(job, dict):
            return old
        existing = str(job.get("recording_id") or "")
        if existing and existing != recording_id:
            return old
        other = jobs.get(recording_id)
        if isinstance(other, dict) and other is not job:
            self._fill(other, job)
            if _STATE_RANK.get(str(job.get("state")), 0) > _STATE_RANK.get(str(other.get("state")), 0):
                other["state"] = job["state"]
            jobs.pop(old, None)
            return recording_id
        jobs.pop(old)
        job["id"] = recording_id
        jobs[recording_id] = job
        return recording_id

    @staticmethod
    def _fill(dst: dict, src: dict) -> None:
        for field in (
            "call_session_id", "call_leg_id", "call_control_id", "recording_id", "connection_id", "from",
            "channels", "format", "file", "sha256", "transcript", "ref", "deleted", "last_error",
        ):
            if not dst.get(field) and src.get(field):
                dst[field] = src[field]
        for field in ("start_ts", "end_ts", "duration_s", "bytes"):
            if not dst.get(field) and src.get(field):
                dst[field] = src[field]
        if src.get("hangup"):
            dst["hangup"] = True
        if src.get("empty_speech"):
            dst["empty_speech"] = True
        if src.get("url") and not dst.get("url"):
            dst["url"] = src["url"]
            dst["url_at"] = src.get("url_at") or 0
        ids = list(dst.get("event_ids") or [])
        for eid in src.get("event_ids") or []:
            if eid not in ids:
                ids.append(eid)
        dst["event_ids"] = ids[-20:]

    def ingest_webhook(self, event: Any) -> dict:
        """Store one reduced Telnyx event.  Idempotent on event id and call ids."""
        if not isinstance(event, dict):
            return {"ok": False, "ignored": True, "duplicate": False, "id": ""}
        if not self.connection_allowed(event.get("connection_id")):
            return self._skip_input("event")
        connection = connection_id_of(event.get("connection_id"))
        etype = str(event.get("event_type") or "")
        eid = _id(event.get("event_id"))
        if etype not in EVENTS or not eid:
            return {"ok": True, "ignored": True, "duplicate": False, "id": ""}
        session = _id(event.get("call_session_id"))
        leg = _id(event.get("call_leg_id"))
        control = _id(event.get("call_control_id"))
        recording_id = _id(event.get("recording_id"))
        if not (session or leg or control or recording_id):
            return {"ok": True, "ignored": True, "duplicate": False, "id": ""}
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            jobs = data["jobs"]
            jid = self._find(jobs, recording_id=recording_id, session=session, leg=leg, control=control)
            if not jid:
                self._make_room(jobs)
                jid = recording_id or ("sess:" + (session or leg or control))
                jobs[jid] = _blank_job(jid, now)
            job = jobs[jid]
            if eid in (job.get("event_ids") or []):
                return {"ok": True, "ignored": False, "duplicate": True, "id": job["id"]}
            job["event_ids"] = (list(job.get("event_ids") or []) + [eid])[-20:]
            job["updated_at"] = now
            if not job.get("connection_id"):
                job["connection_id"] = connection
            self._apply_ids(job, session=session, leg=leg, control=control, recording_id=recording_id)
            caller = _caller(event.get("from"))
            if caller and not job.get("from"):
                job["from"] = caller
            start = _epoch(event.get("start_time") or event.get("recording_started_at"))
            end = _epoch(event.get("end_time") or event.get("recording_ended_at"))
            if start and not job.get("start_ts"):
                job["start_ts"] = start
            if end:
                job["end_ts"] = end
            if etype == "call.hangup":
                job["hangup"] = True
            if etype == "call.recording.error":
                job["state"] = "dead"
                job["last_error"] = "recording-error"
                job["done_at"] = now
            elif etype == "call.recording.saved" or recording_id:
                self._apply_media(job, event, now)
            if job.get("url") and job.get("state") in ("wait", "download"):
                job["state"] = "download"
                job["next_at"] = 0
            elif job.get("state") == "wait" and (etype == "call.recording.saved" or recording_id):
                job["state"] = "download"
                job["next_at"] = 0
            if recording_id:
                jid = self._rekey(jobs, jid, recording_id)
                job = jobs[jid]
            self._write(data)
            log.info("recording ingest %s from=%s", etype, last4(job.get("from") or ""))
            return {"ok": True, "ignored": False, "duplicate": False, "id": job["id"]}

    def ingest_api(self, record: Any) -> dict:
        """Store one recordings-list resource.  Same job as a webhook for that id."""
        if not isinstance(record, dict):
            return {"ok": False, "ignored": True, "duplicate": False, "id": ""}
        if not self.connection_allowed(record.get("connection_id")):
            return self._skip_input("recording")
        connection = connection_id_of(record.get("connection_id"))
        status = str(record.get("status") or "completed")
        if status not in ("completed", ""):
            return {"ok": True, "ignored": True, "duplicate": False, "id": ""}
        recording_id = _id(record.get("id") or record.get("recording_id"))
        session = _id(record.get("call_session_id"))
        leg = _id(record.get("call_leg_id"))
        control = _id(record.get("call_control_id"))
        if not recording_id:
            return {"ok": True, "ignored": True, "duplicate": False, "id": ""}
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            jobs = data["jobs"]
            jid = self._find(jobs, recording_id=recording_id, session=session, leg=leg, control=control)
            if jid and recording_id and jobs[jid].get("recording_id") == recording_id and jobs[jid].get("file"):
                return {"ok": True, "ignored": False, "duplicate": True, "id": jobs[jid]["id"]}
            if not jid:
                self._make_room(jobs)
                jobs[recording_id] = _blank_job(recording_id, now)
                jid = recording_id
            job = jobs[jid]
            before = (job.get("state"), job.get("url"), job.get("recording_id"))
            if not job.get("connection_id"):
                job["connection_id"] = connection
            self._apply_ids(job, session=session, leg=leg, control=control, recording_id=recording_id)
            caller = _caller(record.get("from"))
            if caller and not job.get("from"):
                job["from"] = caller
            start = _epoch(record.get("recording_started_at") or record.get("start_time"))
            end = _epoch(record.get("recording_ended_at") or record.get("end_time"))
            if start and not job.get("start_ts"):
                job["start_ts"] = start
            if end:
                job["end_ts"] = end
            millis = record.get("duration_millis")
            if job.get("duration_s") in (0, None):
                job["duration_s"] = duration_seconds(job.get("start_ts") or 0, job.get("end_ts") or 0, millis)
            channels = record.get("channels")
            if channels in ("dual", "single"):
                job["channels"] = channels
            self._apply_media(job, {"recording_urls": record.get("download_urls") or record.get("recording_urls"),
                                    "format": record.get("format"), "channels": channels}, now)
            if job.get("url") and job.get("state") in ("wait", "download"):
                job["state"] = "download"
                job["next_at"] = 0
            elif job.get("state") == "wait":
                job["state"] = "download"
                job["next_at"] = 0
            if recording_id:
                jid = self._rekey(jobs, jid, recording_id)
                job = jobs[jid]
            progressed = (job.get("state"), job.get("url"), job.get("recording_id")) != before
            job["updated_at"] = now
            self._write(data)
            return {"ok": True, "ignored": False, "duplicate": not progressed, "id": job["id"]}

    @staticmethod
    def _apply_ids(job: dict, *, session: str, leg: str, control: str, recording_id: str) -> None:
        if session and not job.get("call_session_id"):
            job["call_session_id"] = session
        if leg and not job.get("call_leg_id"):
            job["call_leg_id"] = leg
        if control and not job.get("call_control_id"):
            job["call_control_id"] = control
        if recording_id and not job.get("recording_id"):
            job["recording_id"] = recording_id

    def _apply_media(self, job: dict, event: dict, now: float) -> None:
        channels = event.get("channels")
        if channels in ("dual", "single"):
            job["channels"] = channels
        fmt, url = _fmt_from_urls(event.get("recording_urls"))
        if not url and event.get("format") in EXTS and event.get("url"):
            fmt, url = str(event.get("format")), str(event.get("url"))
        if url:
            if url_allowed(url):
                if not job.get("file"):
                    job["url"] = url
                    job["url_at"] = now
                    job["format"] = fmt if fmt in EXTS else "wav"
                    job["url_rejected"] = False
            else:
                job["url_rejected"] = True
                log.info("recording url-rejected from=%s", last4(job.get("from") or ""))

    def _safe_file(self, name: str) -> pathlib.Path | None:
        if not FILE_RE.fullmatch(name or ""):
            return None
        path = (self.dir / name).resolve()
        root = self.dir.resolve()
        if path.parent != root:
            return None
        return path

    def _save_audio(self, recording_id: str, fmt: str, blob: bytes) -> tuple[str, str]:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        ext = fmt if fmt in EXTS else "wav"
        name = f"{file_stem(recording_id)}.{ext}"
        final = self.dir / name
        tmp = self.dir / (name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, final)
        os.chmod(final, 0o600)
        reread = final.read_bytes()
        if reread != blob:
            final.unlink(missing_ok=True)
            raise OSError("recording short write")
        return name, hashlib.sha256(blob).hexdigest()

    def persisted(self, job: dict) -> bool:
        """True only when the audio bytes and the transcript are on disk."""
        path = self._safe_file(str(job.get("file") or ""))
        if path is None or not path.is_file():
            return False
        try:
            mode = path.stat().st_mode & 0o777
            blob = path.read_bytes()
        except OSError:
            return False
        if mode != 0o600 or len(blob) != int(job.get("bytes") or -1) or not blob:
            return False
        if hashlib.sha256(blob).hexdigest() != str(job.get("sha256") or ""):
            return False
        with exclusive_lock(self.lock_path):
            disk = self._read()["jobs"].get(job.get("id") or "")
        if not isinstance(disk, dict):
            return False
        if not str(disk.get("transcript") or ""):
            return False
        if disk.get("transcript") != job.get("transcript"):
            return False
        if not str(disk.get("ref") or "") or disk.get("ref") != job.get("ref"):
            return False
        return True

    def _make_room(self, jobs: dict) -> None:
        """Stay near the cap by dropping the oldest finished job.  A new call is never refused."""
        while len(jobs) >= MAX_JOBS:
            closed = [k for k, j in jobs.items() if isinstance(j, dict) and j.get("state") in CLOSED]
            if not closed:
                log.warning("recording jobs over cap count=%s", len(jobs) + 1)
                return
            oldest = min(closed, key=lambda k: float(jobs[k].get("done_at") or jobs[k].get("created_at") or 0))
            # Drop the index row only.  The inbox may still point at the audio;
            # the retention sweep deletes files after retention_days.
            jobs.pop(oldest, None)

    def redact(self, *, ref: str = "", filename: str = "") -> int:
        """Blank transcript, caller, and download URL after the inbox row is deleted."""
        if not ref and not filename:
            return 0
        with exclusive_lock(self.lock_path):
            data = self._read()
            n = 0
            for job in data["jobs"].values():
                if not isinstance(job, dict):
                    continue
                if ref and job.get("ref") == ref or filename and job.get("file") == filename:
                    job["transcript"] = ""
                    job["from"] = ""
                    job["url"] = ""
                    n += 1
            if n:
                self._write(data)
            return n

    def unlink(self, name: str) -> None:
        path = self._safe_file(name)
        if path is None:
            return
        try:
            path.unlink()
        except OSError:
            pass

    def _update(self, jid: str, fn: Callable[[dict], None]) -> dict | None:
        with exclusive_lock(self.lock_path):
            data = self._read()
            job = data["jobs"].get(jid)
            if not isinstance(job, dict):
                return None
            fn(job)
            job["updated_at"] = now_ts()
            self._write(data)
            return json.loads(json.dumps(job))

    def _fail(self, jid: str, code: str, clock: float, terminal: bool = False) -> None:
        def fn(job: dict) -> None:
            if code not in ("stt-unconfigured", "no-key"):
                job["tries"] = int(job.get("tries") or 0) + 1
            if code == "bad-type":
                job["bad_type_tries"] = int(job.get("bad_type_tries") or 0) + 1
            job["last_error"] = code
            job["lease_until"] = 0
            tries = int(job.get("tries") or 0)
            bad_type_tries = int(job.get("bad_type_tries") or 0)
            is_terminal = terminal
            if code == "bad-type" and not is_terminal:
                # Three backoffs (5, 15, 45), then terminal on the following failure.
                is_terminal = bad_type_tries > BAD_TYPE_TRIES
            elif code not in ("stt-unconfigured", "no-key") and tries >= MAX_TRIES:
                is_terminal = True
            if is_terminal:
                job["state"] = "dead"
                job["done_at"] = clock
            else:
                if code == "bad-type":
                    delay = BAD_TYPE_DELAYS[min(bad_type_tries, len(BAD_TYPE_DELAYS)) - 1]
                elif code in ("stt-unconfigured", "no-key"):
                    delay = 300
                else:
                    step = int(job.get("tries") or 1)
                    delay = min(30 * (2 ** max(0, step - 1)), 1800)
                job["next_at"] = clock + delay
        self._update(jid, fn)
        log.info("recording %s from=%s", code, "none")

    @staticmethod
    def _close_skipped(job: dict, clock: float) -> None:
        """Terminal and silent: no row, no email, and the remote copy is left alone."""
        job["state"] = "skipped"
        job["last_error"] = SKIP_ERROR
        job["done_at"] = clock
        job["lease_until"] = 0
        job["url"] = ""

    async def poll(self, net: Any, now: float | None = None) -> str:
        """Pull one window of recordings.

        The watermark moves only after every page in the window is read.  Hitting
        the page cap leaves the watermark and resumes at the next unread page,
        after re-reading the page before it so a shifted item is not skipped.
        """
        clock = now_ts() if now is None else now
        if not self.allowed:
            return "poll-off"
        with exclusive_lock(self.lock_path):
            stored = self._read()
            watermark = float(stored.get("watermark") or 0)
            page = max(1, int(stored.get("poll_next") or 1))
        since = watermark - POLL_OVERLAP_S if watermark else clock - LOOKBACK_S
        since_iso = datetime.fromtimestamp(max(0, since), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        pages = page
        read = 0
        if page > 1:
            # Boundary page.  Not counted against the cap, so the resume still advances.
            try:
                boundary = await net.list_recordings(since_iso, page - 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("recording poll %s", type(exc).__name__)
                return "poll-failed"
            if not isinstance(boundary, dict) or not isinstance(boundary.get("data"), list):
                return "poll-failed"
            for record in boundary["data"]:
                self.ingest_api(record)
            try:
                pages = max(pages, int(boundary.get("pages") or 1))
            except (TypeError, ValueError):
                pages = max(pages, 1)
        while page <= pages and read < MAX_POLL_PAGES:
            try:
                batch = await net.list_recordings(since_iso, page)
            except Exception as exc:  # noqa: BLE001
                log.warning("recording poll %s", type(exc).__name__)
                return "poll-failed"
            if not isinstance(batch, dict) or not isinstance(batch.get("data"), list):
                return "poll-failed"
            for record in batch["data"]:
                self.ingest_api(record)
            try:
                pages = max(1, int(batch.get("pages") or 1))
            except (TypeError, ValueError):
                pages = 1
            page += 1
            read += 1
        capped = page <= pages
        with exclusive_lock(self.lock_path):
            data = self._read()
            if capped:
                # Next unread page.  The following poll re-reads the page before
                # this one first, so a shift cannot hide the boundary item.
                data["poll_next"] = page
                self._write(data)
                log.info("recording poll-partial count=%s", read)
                return "poll-partial"
            data["watermark"] = clock
            data["poll_next"] = 1
            self._write(data)
        log.info("recording poll-ok")
        return "poll-ok"

    def _due(self, clock: float) -> list[str]:
        with exclusive_lock(self.lock_path):
            out = []
            for jid, job in self._read()["jobs"].items():
                if not isinstance(job, dict):
                    continue
                if job.get("state") in CLOSED or job.get("state") == "wait":
                    continue
                if clock < float(job.get("next_at") or 0):
                    continue
                if clock < float(job.get("lease_until") or 0):
                    continue
                out.append(jid)
            return out

    def _lease(self, jid: str, clock: float) -> dict | None:
        with exclusive_lock(self.lock_path):
            data = self._read()
            job = data["jobs"].get(jid)
            if not isinstance(job, dict) or job.get("state") in CLOSED or job.get("state") == "wait":
                return None
            if not self.connection_allowed(job.get("connection_id")):
                # Defense in depth: a job from before 0.8.1, or a connection removed
                # from the allowlist, is closed without a download, transcript,
                # email, or remote delete.
                self._close_skipped(job, clock)
                self._write(data)
                return None
            if clock < float(job.get("next_at") or 0) or clock < float(job.get("lease_until") or 0):
                return None
            if clock - float(job.get("created_at") or clock) > GIVE_UP_S:
                job["state"] = "dead"
                job["last_error"] = "gave-up"
                job["done_at"] = clock
                job["lease_until"] = 0
                self._write(data)
                return json.loads(json.dumps(job))
            job["lease"] = int(job.get("lease") or 0) + 1
            job["lease_until"] = clock + LEASE_S
            self._write(data)
            return json.loads(json.dumps(job))

    def _release(self, jid: str, token: int) -> None:
        def fn(job: dict) -> None:
            if int(job.get("lease") or 0) == token:
                job["lease_until"] = 0
        self._update(jid, fn)

    def _expire_waiting(self, clock: float) -> None:
        """A call that never produces a recording still becomes a dead job, not a silent gap."""
        with exclusive_lock(self.lock_path):
            data = self._read()
            dirty = False
            for job in data["jobs"].values():
                if not isinstance(job, dict) or job.get("state") != "wait":
                    continue
                if not self.connection_allowed(job.get("connection_id")):
                    self._close_skipped(job, clock)
                    dirty = True
                    continue
                if clock - float(job.get("created_at") or clock) <= GIVE_UP_S:
                    continue
                job["state"] = "dead"
                job["last_error"] = "no-recording"
                job["done_at"] = clock
                dirty = True
            if dirty:
                self._write(data)

    def _unsurfaced_dead(self) -> list[str]:
        with exclusive_lock(self.lock_path):
            out = []
            for jid, job in self._read()["jobs"].items():
                if (isinstance(job, dict) and job.get("state") == "dead" and not job.get("ref")
                        and not job.get("surfaced") and self.connection_allowed(job.get("connection_id"))):
                    out.append(jid)
            return out

    def _siblings_open(self, session: str, recording_id: str) -> bool:
        """True when another recording of this call is not finished yet."""
        if not session:
            return False
        for job in self.jobs().values():
            if job.get("call_session_id") != session:
                continue
            other = str(job.get("recording_id") or "")
            if not other or other == recording_id:
                continue
            if job.get("state") not in CLOSED:
                return True
        return False

    async def advance(self, net: Any, attach: Callable[[dict], Awaitable[str]], now: float | None = None,
                      delete_gate: str = "hold") -> list[str]:
        """Move every due job as far as it can.  One recording becomes one attach."""
        clock = now_ts() if now is None else now
        codes: list[str] = []
        self._expire_waiting(clock)
        for jid in self._unsurfaced_dead():
            job = self.get(jid)
            if job:
                await self._surface_dead(job, attach)
                codes.append("recording dead")
        for jid in self._due(clock):
            codes.extend(await self._advance_one(jid, net, attach, clock, delete_gate))
        return codes

    async def _advance_one(self, jid: str, net: Any, attach: Callable[[dict], Awaitable[str]], clock: float,
                           delete_gate: str = "hold") -> list[str]:
        job = self._lease(jid, clock)
        if job is None:
            return []
        if job.get("state") == "dead" and job.get("last_error") == "gave-up":
            await self._surface_dead(job, attach)
            return ["recording gave-up"]
        token = int(job.get("lease") or 0)
        codes: list[str] = []
        try:
            while True:
                fresh = self.get(jid)
                if not fresh or int(fresh.get("lease") or 0) != token:
                    return codes
                if not self.connection_allowed(fresh.get("connection_id")):
                    self._update(jid, lambda j: self._close_skipped(j, clock))
                    codes.append("recording " + SKIP_ERROR)
                    return codes
                if fresh.get("state") in CLOSED:
                    if fresh.get("state") == "dead":
                        await self._surface_dead(fresh, attach)
                    return codes
                state = fresh.get("state")
                if state == "download":
                    err = await self._step_download(jid, fresh, net, clock)
                    codes.append("recording " + (err or "saved"))
                    if err:
                        await self._surface_if_dead(jid, attach)
                        return codes
                    continue
                if state == "transcribe":
                    err = await self._step_stt(jid, fresh, net, clock)
                    codes.append("recording " + (err or "transcribed"))
                    if err:
                        await self._surface_if_dead(jid, attach)
                        return codes
                    continue
                if state == "attach":
                    err = await self._step_attach(jid, fresh, attach, clock)
                    codes.append("recording " + (err or "attached"))
                    if err:
                        await self._surface_if_dead(jid, attach)
                        return codes
                    continue
                if state == "delete":
                    err = await self._step_delete(jid, fresh, net, clock, delete_gate)
                    codes.append("recording " + (err or "telnyx-deleted"))
                    return codes
                return codes
        finally:
            self._release(jid, token)

    async def _step_download(self, jid: str, job: dict, net: Any, clock: float) -> str:
        url = str(job.get("url") or "")
        if url and clock - float(job.get("url_at") or 0) > URL_TTL_S:
            url = ""
        if url and not url_allowed(url):
            job = self._update(jid, lambda j: j.update(url="", url_rejected=True)) or job
            url = ""
        if not url and job.get("recording_id") and getattr(net, "refresh", None):
            try:
                fresh = await net.refresh(str(job["recording_id"]))
            except Exception as exc:  # noqa: BLE001
                log.warning("recording refresh %s", type(exc).__name__)
                fresh = ""
            if isinstance(fresh, str) and url_allowed(fresh):
                url = fresh
                self._update(jid, lambda j: j.update(url=url, url_at=clock, url_rejected=False))
            elif isinstance(fresh, str) and fresh:
                self._fail(jid, "ssrf", clock, terminal=True)
                return "ssrf"
        if not url:
            code = "ssrf" if job.get("url_rejected") else "url-missing"
            self._fail(jid, code, clock, terminal=code == "ssrf")
            return code
        if not url_allowed(url):
            self._fail(jid, "ssrf", clock, terminal=True)
            return "ssrf"
        try:
            fetched = await net.fetch(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording fetch %s", type(exc).__name__)
            self._fail(jid, "download", clock)
            return "download"
        err, blob = interpret_fetch(fetched)
        if err:
            self._fail(jid, err, clock, terminal=err in TERMINAL_DOWNLOAD or err == "empty")
            return err
        try:
            name, digest = self._save_audio(str(job.get("recording_id") or jid), str(job.get("format") or "wav"), blob)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording save %s", type(exc).__name__)
            self._fail(jid, "download", clock)
            return "download"
        self._update(jid, lambda j: j.update(
            file=name, bytes=len(blob), sha256=digest, url="", state="transcribe", next_at=0, last_error="",
        ))
        log.info("recording saved from=%s", last4(job.get("from") or ""))
        return ""

    async def _step_stt(self, jid: str, job: dict, net: Any, clock: float) -> str:
        path = self._safe_file(str(job.get("file") or ""))
        if path is None or not path.is_file():
            self._fail(jid, "file-missing", clock, terminal=True)
            return "file-missing"
        try:
            blob = path.read_bytes()
        except OSError:
            self._fail(jid, "file-missing", clock, terminal=True)
            return "file-missing"
        try:
            result = await net.stt(blob, str(job.get("format") or "wav"), str(job.get("channels") or "single"))
        except Exception as exc:  # noqa: BLE001
            log.warning("recording stt %s", type(exc).__name__)
            self._fail(jid, "stt", clock)
            return "stt"
        if isinstance(result, dict) and result.get("error"):
            code = str(result.get("error") or "stt")
            self._fail(jid, code if code == "stt-unconfigured" else "stt", clock, terminal=False)
            return code if code == "stt-unconfigured" else "stt"
        if not isinstance(result, dict):
            self._fail(jid, "stt", clock)
            return "stt"
        text = turns_from_stt(result, str(job.get("channels") or "single")).strip()
        seconds = duration_seconds(float(job.get("start_ts") or 0), float(job.get("end_ts") or 0),
                                   job.get("duration_millis"), result.get("duration"))
        if not job.get("duration_s"):
            seconds = seconds or duration_seconds(0, 0, None, result.get("duration"))
        else:
            seconds = int(job.get("duration_s") or 0) or seconds
        empty = not text
        if empty:
            text = empty_notice(seconds)
        self._update(jid, lambda j: j.update(
            transcript=text, empty_speech=empty, duration_s=seconds, state="attach", next_at=0, last_error="",
        ))
        log.info("recording transcribed empty=%s from=%s", "yes" if empty else "no", last4(job.get("from") or ""))
        return ""

    async def _step_attach(self, jid: str, job: dict, attach: Callable[[dict], Awaitable[str]], clock: float) -> str:
        current = dict(self.get(jid) or job)
        current["siblings_open"] = self._siblings_open(str(current.get("call_session_id") or ""),
                                                       str(current.get("recording_id") or ""))
        current["failed"] = False
        try:
            ref = await attach(current)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording attach %s", type(exc).__name__)
            ref = ""
        if not re.fullmatch(r"^CC-[ACDEFGHJKLMNPQRTUVWXY34679]{5}$", str(ref or "")):
            self._fail(jid, "attach", clock)
            return "attach"
        self._update(jid, lambda j: j.update(ref=str(ref), state="delete", next_at=0, last_error=""))
        log.info("recording attached ref=%s", ref)
        return ""

    async def _step_delete(self, jid: str, job: dict, net: Any, clock: float, delete_gate: str = "hold") -> str:
        current = self.get(jid) or job
        # Never delete a recording from a connection outside the allowlist.
        if not (self.connection_allowed(current.get("connection_id"))
                and self.connection_allowed(job.get("connection_id"))):
            self._update(jid, lambda j: self._close_skipped(j, clock))
            return SKIP_ERROR
        if not self.persisted(current):
            self._fail(jid, "persist", clock)
            return "persist"
        # Remote delete only when the same dry_run gate that sends mail is explicitly off,
        # and the option is on.  Unknown or unavailable holds the delete.
        if not getattr(net, "delete_remote", True):
            self._update(jid, lambda j: j.update(deleted="off", state="done", done_at=clock, lease_until=0))
            log.info("recording telnyx-delete off ref=%s", current.get("ref"))
            return "telnyx-delete-off"
        if delete_gate != "send":
            if delete_gate == "dry_run":
                self._update(jid, lambda j: j.update(deleted="dry_run", state="done", done_at=clock, lease_until=0))
                log.info("recording telnyx-delete dry-run ref=%s", current.get("ref"))
                return "telnyx-delete-dry-run"

            def hold(row: dict) -> None:
                row["last_error"] = "dry-run-unknown"
                row["next_at"] = clock + 60
                row["lease_until"] = 0
            self._update(jid, hold)
            log.info("recording telnyx-delete held ref=%s", current.get("ref"))
            return "telnyx-delete-held"
        deleter = getattr(net, "delete", None)
        if deleter is None:
            self._fail(jid, "no-key", clock)
            return "no-key"
        # Re-check right before the remote call, against the stored job.
        if not self.connection_allowed((self.get(jid) or {}).get("connection_id")):
            self._update(jid, lambda j: self._close_skipped(j, clock))
            return SKIP_ERROR
        rid = str(current.get("recording_id") or "")
        if not ID_RE.fullmatch(rid):
            self._fail(jid, "delete-no-id", clock, terminal=True)
            return "delete-no-id"
        # Re-check the durable write immediately before the remote delete.
        if not self.persisted(self.get(jid) or current):
            self._fail(jid, "persist", clock)
            return "persist"
        try:
            ok = await deleter(rid)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording delete %s", type(exc).__name__)
            ok = False
        if not ok:
            self._fail(jid, "delete", clock)
            return "delete"
        self._update(jid, lambda j: j.update(deleted="yes", state="done", done_at=clock, lease_until=0))
        log.info("recording telnyx-deleted ref=%s", current.get("ref"))
        return ""

    async def _surface_if_dead(self, jid: str, attach: Callable[[dict], Awaitable[str]]) -> None:
        job = self.get(jid)
        if job and job.get("state") == "dead":
            await self._surface_dead(job, attach)

    async def _surface_dead(self, job: dict, attach: Callable[[dict], Awaitable[str]]) -> None:
        """A terminal failure still becomes one call row.  The remote copy stays."""
        if job.get("ref") or job.get("surfaced"):
            return
        if not self.connection_allowed(job.get("connection_id")):
            return
        code = str(job.get("last_error") or "failed")
        surfaced = dict(job)
        surfaced["failed"] = True
        surfaced["error_code"] = code
        surfaced["transcript"] = ""
        surfaced["siblings_open"] = self._siblings_open(str(job.get("call_session_id") or ""),
                                                        str(job.get("recording_id") or ""))
        try:
            ref = await attach(surfaced)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording dead-attach %s", type(exc).__name__)
            return
        if not re.fullmatch(r"^CC-[ACDEFGHJKLMNPQRTUVWXY34679]{5}$", str(ref or "")):
            return

        def fn(row: dict) -> None:
            row["ref"] = str(ref)
            row["surfaced"] = True
            row["failed"] = True
            row["error_code"] = code
            row["transcript"] = ""
        self._update(str(job.get("id") or ""), fn)
        log.info("recording dead ref=%s code=%s", ref, code)

    def sweep_retention(self, now: float | None = None) -> int:
        clock = now_ts() if now is None else now
        live: set[str] = set()
        with exclusive_lock(self.lock_path):
            data = self._read()
            for job in data["jobs"].values():
                if isinstance(job, dict) and job.get("file"):
                    live.add(str(job["file"]))
            stale = [k for k, j in data["jobs"].items()
                     if isinstance(j, dict) and j.get("state") in CLOSED
                     and clock - float(j.get("done_at") or clock) > self.retention_s]
            for key in stale:
                data["jobs"].pop(key, None)
            if stale:
                self._write(data)
        removed = 0
        if self.dir.is_dir():
            for entry in self.dir.iterdir():
                try:
                    if (entry.is_file() and FILE_RE.fullmatch(entry.name) and entry.name not in live
                    and clock - entry.stat().st_mtime > self.retention_s):
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed


class HttpNet:
    """Telnyx + xAI calls.  The key is read from the constructor and never logged."""

    def __init__(self, session: Any, telnyx_api_key: str, xai_api_key: str,
                 stt_model: str = "grok-voice-transcribe-2.0", delete_remote: bool = True):
        self.session = session
        self.telnyx_api_key = telnyx_api_key
        self.xai_api_key = xai_api_key
        self.stt_model = stt_model or "grok-voice-transcribe-2.0"
        self.delete_remote = bool(delete_remote)

    def _telnyx_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.telnyx_api_key}", "Accept": "application/json"}

    async def list_recordings(self, created_gte: str, page: int) -> dict | None:
        if not self.telnyx_api_key or self.session is None:
            return None
        from aiohttp import ClientTimeout

        params = {"filter[created_at][gte]": created_gte, "page[size]": "50", "page[number]": str(page)}
        try:
            async with self.session.get(
                "https://api.telnyx.com/v2/recordings", headers=self._telnyx_headers(), params=params,
                allow_redirects=False, timeout=ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    log.warning("recording list http %s", resp.status)
                    await _discard(resp)
                    return None
                body = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording list %s", type(exc).__name__)
            return None
        if not isinstance(body, dict):
            return None
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
        try:
            pages = int(meta.get("total_pages") or 1)
        except (TypeError, ValueError):
            pages = 1
        data = body.get("data") if isinstance(body.get("data"), list) else []
        return {"data": data, "pages": pages}

    async def refresh(self, recording_id: str) -> str:
        if not self.telnyx_api_key or self.session is None or not ID_RE.fullmatch(recording_id):
            return ""
        from aiohttp import ClientTimeout

        try:
            async with self.session.get(
                f"https://api.telnyx.com/v2/recordings/{recording_id}", headers=self._telnyx_headers(),
                allow_redirects=False, timeout=ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    await _discard(resp)
                    return ""
                body = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording get %s", type(exc).__name__)
            return ""
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return ""
        _fmt, url = _fmt_from_urls(data.get("download_urls") or data.get("recording_urls"))
        return url if url_allowed(url) else ""

    async def fetch(self, url: str) -> Any:
        if not url_allowed(url) or self.session is None:
            return {"error": "ssrf"}
        host = (urlsplit(url).hostname or "")
        public = host_is_public(host)
        if public is False:
            log.info("recording fetch blocked")
            return {"error": "ssrf"}
        if public is None:
            return {"error": "download"}
        from aiohttp import ClientTimeout

        try:
            async with self.session.get(url, allow_redirects=False, timeout=ClientTimeout(total=60)) as resp:
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                if resp.status in (301, 302, 303, 307, 308):
                    await _discard(resp)
                    return {"error": "redirect", "http_status": resp.status, "content_type": ctype, "bytes": 0}
                if resp.status != 200:
                    log.warning("recording fetch http %s type=%s", resp.status, content_type_for_log(ctype))
                    await _discard(resp)
                    return {"error": "download", "http_status": resp.status, "content_type": ctype, "bytes": 0}
                if content_type_hard_reject(ctype):
                    await _discard(resp)
                    return {"error": "bad-type", "check": "header", "http_status": resp.status, "content_type": ctype, "bytes": 0}
                declared = resp.headers.get("Content-Length") or ""
                if declared.isdigit() and int(declared) > MAX_AUDIO:
                    return {"error": "oversize", "http_status": resp.status, "content_type": ctype, "bytes": 0}
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_AUDIO:
                        return {"error": "oversize", "http_status": resp.status, "content_type": ctype, "bytes": len(buf)}
                blob = bytes(buf)
                err, check = classify_audio(ctype, blob)
                if err:
                    return {"error": err, "check": check, "http_status": resp.status, "content_type": ctype, "bytes": len(blob)}
                return blob
        except Exception as exc:  # noqa: BLE001
            log.warning("recording fetch %s", type(exc).__name__)
            return {"error": "download"}

    async def stt(self, blob: bytes, fmt: str, channels: str) -> dict | None:
        if not self.xai_api_key or self.session is None:
            return {"error": "stt-unconfigured"}
        from aiohttp import ClientTimeout, FormData

        form = FormData()
        form.add_field("model", self.stt_model)
        form.add_field("language", "en")
        if channels == "dual":
            form.add_field("multichannel", "true")
        mime = "audio/wav" if fmt == "wav" else "audio/mpeg"
        # The file field is last, which the STT API requires.
        form.add_field("file", blob, filename=f"call.{fmt if fmt in EXTS else 'wav'}", content_type=mime)
        try:
            async with self.session.post(
                "https://api.x.ai/v1/stt", data=form,
                headers={"Authorization": f"Bearer {self.xai_api_key}"},
                allow_redirects=False, timeout=ClientTimeout(total=90),
            ) as resp:
                if resp.status != 200:
                    log.warning("recording stt http %s", resp.status)
                    await _discard(resp)
                    return None
                body = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("recording stt %s", type(exc).__name__)
            return None
        return body if isinstance(body, dict) else None

    async def delete(self, recording_id: str) -> bool:
        if not self.telnyx_api_key or self.session is None or not ID_RE.fullmatch(recording_id):
            return False
        from aiohttp import ClientTimeout

        try:
            async with self.session.delete(
                f"https://api.telnyx.com/v2/recordings/{recording_id}", headers=self._telnyx_headers(),
                allow_redirects=False, timeout=ClientTimeout(total=20),
            ) as resp:
                await _discard(resp)
                # 404: already gone, which is the outcome delete is for.
                if resp.status in (200, 202, 204, 404):
                    return True
                log.warning("recording delete http %s", resp.status)
                return False
        except Exception as exc:  # noqa: BLE001
            log.warning("recording delete %s", type(exc).__name__)
            return False
