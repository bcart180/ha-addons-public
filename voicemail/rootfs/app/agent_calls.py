"""Phone-agent call sessions (0.7.0).

One row per phone-agent call, keyed by an opaque call key that CallCatch Line
derives (a SHA-256 of the per-call header or MCP session id).  A row is created
the first time the gateway sees the call, so there is a record before any
hangup.  It is finalized exactly once by whichever comes first:

* ``agent``   - the voice agent called take_message / request_callback,
* ``hook``    - a call-ended webhook arrived (works no matter who hung up),
* ``ended``   - the MCP client closed its session (only when enabled),
* ``timeout`` - no activity for ``inactivity_s`` seconds (server finalizer).

When a call-ended hook is expected, an agent or timeout finalization waits up
to ``hook_grace_s`` for the hook so the full transcript can ride along.  The
``notified`` flag is set inside the same locked write that finalizes, so the
notification is enqueued at most once per call.  Anything that arrives after
that (late hook, late agent final, duplicate hook) only updates the stored
record.

Stored text is caller-supplied and untrusted.  It is cleaned and capped here
and is never interpreted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import call_summary
import contacts_sync
from inbox_store import atomic_write_bytes, exclusive_lock

VERSION = 1
KEY_RE = re.compile(r"^[a-f0-9]{32,64}$")
REF_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"  # no 0/O, 1/I, 2/Z, 5/S, 8/B
URGENCY = ("low", "normal", "urgent")

FIELD_MAX = {
    "caller_name": 60,
    "callback_number": 20,
    "reason": 280,
    "best_time": 80,
    "preferred_window": 80,
}
NOTE_MAX = 600
NOTES_MAX = 12
TRANSCRIPT_MAX = 60_000
TURNS_MAX = 400
TURN_TEXT_MAX = 500  # one spoken turn, as the agent wrote it (0.7.2)
SUMMARY_MAX = 1200
CONV_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")
START_RE = re.compile(r"^[A-Za-z0-9_.:+\-/ ]{1,64}$")
START_MIN, START_MAX = 1577836800, 4102444800  # 2020-01-01 .. 2100-01-01 UTC
SPEAKERS = {"caller": "caller", "user": "caller", "customer": "caller", "agent": "agent", "assistant": "agent"}
MAX_ROWS = 500
KEEP_FINAL_S = 30 * 24 * 3600

STATE_OPEN = "open"
STATE_WAIT_HOOK = "awaiting_hook"
STATE_WAIT_REC = "awaiting_recording"
STATE_FINAL = "final"
MATCH_WINDOW_S = 120


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any, limit: int) -> str:
    text = call_summary.plain_text(str(value or "")[: limit * 4])
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def clean_number(value: Any) -> str:
    raw = call_summary.strip_format(str(value or "")[:40])
    d = re.sub(r"\D", "", raw)
    if len(d) == 10:
        d = "1" + d
    if 11 <= len(d) <= 15:
        return "+" + d
    return ""


def clean_fields(raw: Any) -> dict:
    """Allow-listed, capped fields.  Unknown keys are dropped."""
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    for key, limit in FIELD_MAX.items():
        if key not in raw or raw.get(key) in (None, ""):
            continue
        if key == "callback_number":
            num = clean_number(raw.get(key))
            if num:
                out[key] = num
            continue
        text = clean_text(raw.get(key), limit)
        if text:
            out[key] = text
    urgency = str(raw.get("urgency") or "").strip().lower()
    if urgency in URGENCY:
        out["urgency"] = urgency
    note = clean_text(raw.get("notes"), NOTE_MAX)
    if note:
        out["notes"] = note
    if raw.get("callback_requested") is True:
        out["callback_requested"] = True
    return out


def clean_transcript(raw: Any) -> str:
    """Hook transcript: a string or a list of {role, text} turns."""
    if isinstance(raw, str):
        lines = [raw]
    elif isinstance(raw, list):
        lines = []
        for turn in raw[:TURNS_MAX]:
            if isinstance(turn, dict):
                role = str(turn.get("role") or turn.get("speaker") or "").strip().lower()
                label = "Caller" if role in ("user", "caller", "customer") else "Assistant"
                text = clean_text(turn.get("text") or turn.get("content") or "", 2000)
                if text:
                    lines.append(f"{label}: {text}")
            elif isinstance(turn, str) and turn.strip():
                lines.append(clean_text(turn, 2000))
    else:
        return ""
    out = "\n".join(line for line in lines if line)
    out = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", out)
    return out[:TRANSCRIPT_MAX].strip()


def clean_turn(raw: Any) -> dict | None:
    """Agent-written turn: {"speaker": caller|agent, "text": ...}.  None if unusable."""
    if not isinstance(raw, dict):
        return None
    who = SPEAKERS.get(str(raw.get("speaker") or raw.get("role") or "").strip().lower())
    text = clean_text(raw.get("text"), TURN_TEXT_MAX)
    if not who or not text:
        return None
    return {"speaker": who, "text": text}


def clean_conversation_id(raw: Any) -> str:
    text = str(raw or "").strip()
    return text if CONV_RE.fullmatch(text) and "{" not in text else ""


def clean_call_start(raw: Any) -> str:
    """Platform call-start variable, kept as given (format unpublished)."""
    text = str(raw or "").strip()
    return text if START_RE.fullmatch(text) and "{" not in text else ""


def parse_call_start(text: str) -> float:
    """Epoch seconds from epoch s/ms or ISO 8601 with a zone.  0 if unknown.

    A time without a zone is not guessed at: 0, and the raw text is shown."""
    t = clean_call_start(text)
    if not t:
        return 0.0
    ts = 0.0
    if re.fullmatch(r"\d{10}(\.\d+)?", t):
        ts = float(t)
    elif re.fullmatch(r"\d{13}", t):
        ts = int(t) / 1000.0
    else:
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00").replace(" ", "T", 1))
        except ValueError:
            return 0.0
        if dt.tzinfo is None:
            return 0.0
        ts = dt.timestamp()
    return ts if START_MIN <= ts <= START_MAX else 0.0


def start_is_precise(text: Any) -> bool:
    """Seconds-resolution start (epoch s/ms, or ISO 8601 with seconds).  Only
    such a value may join two requests into one call by caller + start (L1)."""
    t = clean_call_start(text)
    if re.fullmatch(r"\d{10}(\.\d{1,6})?", t):
        return START_MIN <= float(t) <= START_MAX
    if re.fullmatch(r"\d{13}", t):
        return START_MIN <= int(t) / 1000 <= START_MAX
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?", t):
        return False
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return START_MIN <= dt.timestamp() <= START_MAX


def turns_text(row: dict) -> str:
    """Agent-written turn log as plain lines ("Caller: ..." / "Agent: ...")."""
    lines = []
    for t in row.get("turns") or []:
        label = "Caller" if t.get("speaker") == "caller" else "Agent"
        lines.append(f"{label}: {t.get('text') or ''}")
    return "\n".join(lines)


def new_ref() -> str:
    return "CC-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(5))


class AgentCalls:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.lock_path = path.with_suffix(".lock")

    # storage -------------------------------------------------------------
    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": VERSION, "calls": {}}
        if not isinstance(data, dict) or not isinstance(data.get("calls"), dict):
            return {"version": VERSION, "calls": {}}
        return data

    def _write(self, data: dict) -> None:
        atomic_write_bytes(self.path, json.dumps(data, sort_keys=True).encode("utf-8"))

    def _prune(self, data: dict, now: float) -> None:
        calls = data["calls"]
        for key in list(calls):
            row = calls[key]
            if row.get("state") == STATE_FINAL and now - float(row.get("finalized_at") or now) > KEEP_FINAL_S:
                del calls[key]
        if len(calls) > MAX_ROWS:
            finals = sorted(
                (k for k, v in calls.items() if v.get("state") == STATE_FINAL),
                key=lambda k: float(calls[k].get("finalized_at") or 0),
            )
            for key in finals[: len(calls) - MAX_ROWS]:
                del calls[key]

    def get(self, key: str) -> dict | None:
        with exclusive_lock(self.lock_path):
            row = self._read()["calls"].get(key)
            return copy.deepcopy(row) if isinstance(row, dict) else None

    def by_ref(self, ref: str) -> dict | None:
        want = str(ref or "")
        if not want:
            return None
        with exclusive_lock(self.lock_path):
            for row in self._read()["calls"].values():
                if isinstance(row, dict) and str(row.get("ref") or "") == want:
                    return copy.deepcopy(row)
        return None

    def patch_fields(self, ref: str, fields: dict) -> dict | None:
        """Write identity fields back onto the stored row for this CallCatch ref."""
        want = str(ref or "")
        if not want or not isinstance(fields, dict):
            return None
        with exclusive_lock(self.lock_path):
            data = self._read()
            for row in data["calls"].values():
                if not isinstance(row, dict) or str(row.get("ref") or "") != want:
                    continue
                cur = row.get("fields")
                if not isinstance(cur, dict):
                    cur = {}
                    row["fields"] = cur
                for key, value in fields.items():
                    cur[key] = value
                self._write(data)
                return copy.deepcopy(row)
        return None

    def all(self) -> list[dict]:
        with exclusive_lock(self.lock_path):
            return [copy.deepcopy(v) for v in self._read()["calls"].values()]

    # helpers -------------------------------------------------------------
    @staticmethod
    def _new_row(key: str, now: float, refs: set) -> dict:
        ref = new_ref()
        while ref in refs:
            ref = new_ref()
        return {
            "key": key,
            "ref": ref,
            "state": STATE_OPEN,
            "created_at": now,
            "last_activity": now,
            "fields": {},
            "notes": [],
            "transcript": "",
            "agent_final": False,
            "final_kind": "",
            "hook_received": False,
            "hook_ids": [],
            "final_source": "",
            "finalized_at": 0,
            "wait_until": 0,
            "hung_up_early": False,
            "notified": False,
            "late_updates": 0,
            "turns": [],
            "turns_dropped": 0,
            "summary": "",
            "conversation_id": "",
            "call_start": "",
            "call_start_ts": 0,
            "telnyx_session": "",
            "telnyx_leg": "",
            "telnyx_control": "",
            "telnyx_recording": "",
            "telnyx_start_ts": 0,
            "telnyx_end_ts": 0,
            "verbatim_at": 0,
            "duration_s": 0,
            "recording_file": "",
            "recording_parts": [],
            "capture_failed": False,
            "capture_error": "",
        }

    @staticmethod
    def _merge(row: dict, fields: dict, frozen: bool = False) -> bool:
        """Merge cleaned fields.  After final (``frozen``) the emailed fields
        stay as sent; a changed value is kept as a "Later" note (I2)."""
        changed = False
        note = fields.pop("notes", "")
        if fields.pop("callback_requested", False) and not row["fields"].get("callback_requested"):
            row["fields"]["callback_requested"] = True
            changed = True
        for k, v in fields.items():
            if row["fields"].get(k) == v:
                continue
            if frozen and row["fields"].get(k):
                later = clean_text(f"Later {k.replace('_', ' ')}: {v}", NOTE_MAX)
                if later not in row["notes"]:
                    row["notes"] = (row["notes"] + [later])[-NOTES_MAX:]
                    changed = True
                continue
            row["fields"][k] = v
            changed = True
        if note and note not in row["notes"]:
            row["notes"] = (row["notes"] + [note])[-NOTES_MAX:]
            changed = True
        return changed

    @staticmethod
    def _extras(row: dict, turn: Any, summary: Any, conversation_id: Any, call_start: Any = "") -> bool:
        """Append-only turn log, latest summary, conversation id, call start (0.7.2)."""
        changed = False
        start = clean_call_start(call_start)
        if start and not row.get("call_start"):
            row["call_start"] = start
            row["call_start_ts"] = parse_call_start(start)
            changed = True
        conv = clean_conversation_id(conversation_id)
        if conv and not row.get("conversation_id"):
            row["conversation_id"] = conv
            changed = True
        t = clean_turn(turn) if turn is not None else None
        if t:
            turns = row.setdefault("turns", [])
            if turns and turns[-1] == t:
                # Same speaker and text as the turn just before it: treated as
                # the agent retrying the tool call, so one copy is kept.  A
                # caller who truly says the same thing twice in a row is
                # logged once (accepted, audit I3); the tool has no retry id.
                pass
            elif len(turns) >= TURNS_MAX or sum(len(x["text"]) for x in turns) + len(t["text"]) > TRANSCRIPT_MAX:
                row["turns_dropped"] = int(row.get("turns_dropped") or 0) + 1
            else:
                turns.append(t)
                changed = True
        text = clean_text(summary, SUMMARY_MAX) if summary else ""
        if text and text != row.get("summary"):
            row["summary"] = text
            changed = True
        return changed

    def _row(self, data: dict, key: str, now: float) -> dict:
        calls = data["calls"]
        row = calls.get(key)
        if not isinstance(row, dict):
            refs = {str(v.get("ref")) for v in calls.values()}
            row = self._new_row(key, now, refs)
            calls[key] = row
        return row

    @staticmethod
    def _finalize(row: dict, source: str, now: float) -> bool:
        """Mark final.  True only for the first finalization (enqueue once)."""
        if row["state"] == STATE_FINAL:
            return False
        row["state"] = STATE_FINAL
        row["final_source"] = source
        row["finalized_at"] = now
        row["hung_up_early"] = not row["agent_final"]
        first = not row["notified"]
        row["notified"] = True
        return first

    # operations ----------------------------------------------------------
    def touch(self, key: str, fields: Any = None, caller_hint: str = "", turn: Any = None,
              summary: Any = None, conversation_id: Any = "", call_start: Any = "") -> dict:
        """open / update_call: create if new, merge fields, bump activity."""
        if not KEY_RE.fullmatch(key or ""):
            raise ValueError("bad-key")
        clean = clean_fields(fields)
        hint = clean_number(caller_hint)
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            self._prune(data, now)
            row = self._row(data, key, now)
            late = row["state"] == STATE_FINAL
            if hint and not row["fields"].get("caller_id"):
                row["fields"]["caller_id"] = hint
            changed = self._merge(row, clean, frozen=late)
            changed = self._extras(row, turn, summary, conversation_id, call_start) or changed
            if not late:
                row["last_activity"] = now
            elif changed:
                row["late_updates"] = int(row.get("late_updates") or 0) + 1
            self._write(data)
            return {"row": copy.deepcopy(row), "late": late, "changed": changed}

    def agent_final(self, key: str, kind: str, fields: Any, caller_hint: str = "", hook_expected: bool = False,
                    hook_grace_s: int = 120, summary: Any = None, conversation_id: Any = "",
                    call_start: Any = "", recording_hold: bool = False, recording_grace_s: int = 180) -> dict:
        if kind not in ("message", "callback"):
            raise ValueError("bad-kind")
        if not KEY_RE.fullmatch(key or ""):
            raise ValueError("bad-key")
        clean = clean_fields(fields)
        if kind == "callback":
            clean["callback_requested"] = True
        hint = clean_number(caller_hint)
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            self._prune(data, now)
            row = self._row(data, key, now)
            if hint and not row["fields"].get("caller_id"):
                row["fields"]["caller_id"] = hint
            late = row["state"] == STATE_FINAL
            changed = self._merge(row, clean, frozen=late)
            changed = self._extras(row, None, summary, conversation_id, call_start) or changed
            first = False
            if late:
                if changed:
                    row["late_updates"] = int(row.get("late_updates") or 0) + 1
            else:
                row["agent_final"] = True
                row["final_kind"] = kind
                row["last_activity"] = now
                need_hook = bool(hook_expected) and not row["hook_received"]
                need_rec = bool(recording_hold) and not row.get("verbatim_at")
                if need_hook or need_rec:
                    grace = 0
                    if need_hook:
                        grace = max(grace, int(hook_grace_s))
                    if need_rec:
                        grace = max(grace, int(recording_grace_s))
                    row["state"] = STATE_WAIT_REC if need_rec else STATE_WAIT_HOOK
                    row["wait_until"] = now + max(0, grace)
                else:
                    first = self._finalize(row, "agent", now)
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first, "late": late, "changed": changed}

    def hook(self, key: str, hook_id: str, transcript: Any, caller_hint: str = "", ended_at: float | None = None,
             recording_hold: bool = False, recording_grace_s: int = 180) -> dict:
        """Call-ended webhook.  Idempotent per hook id and per call."""
        if not KEY_RE.fullmatch(key or ""):
            raise ValueError("bad-key")
        text = clean_transcript(transcript)
        hint = clean_number(caller_hint)
        hid = re.sub(r"[^A-Za-z0-9_\-:.]", "", str(hook_id or ""))[:80]
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            self._prune(data, now)
            row = self._row(data, key, now)
            duplicate = bool(hid) and hid in row["hook_ids"]
            if hid and not duplicate:
                row["hook_ids"] = (row["hook_ids"] + [hid])[-5:]
            if hint and not row["fields"].get("caller_id"):
                row["fields"]["caller_id"] = hint
            changed = False
            if text and not row.get("verbatim_at") and len(text) > len(row.get("transcript") or ""):
                row["transcript"] = text
                changed = True
            late = row["state"] == STATE_FINAL
            row["hook_received"] = True
            first = False
            if not late:
                if recording_hold and not row.get("verbatim_at"):
                    row["state"] = STATE_WAIT_REC
                    row["wait_until"] = max(float(row.get("wait_until") or 0), now + max(0, int(recording_grace_s)))
                else:
                    first = self._finalize(row, "hook", now)
            elif changed and not duplicate:
                row["late_updates"] = int(row.get("late_updates") or 0) + 1
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first, "late": late, "duplicate": duplicate,
                    "changed": changed}

    def ended(self, key: str, hook_expected: bool = False, hook_grace_s: int = 120,
              recording_hold: bool = False, recording_grace_s: int = 180) -> dict:
        """MCP session closed by the client (treated as call ended)."""
        if not KEY_RE.fullmatch(key or ""):
            raise ValueError("bad-key")
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            row = data["calls"].get(key)
            if not isinstance(row, dict):
                return {"row": None, "notify": False}
            first = False
            if row["state"] == STATE_OPEN:
                need_hook = bool(hook_expected) and not row["hook_received"]
                need_rec = bool(recording_hold) and not row.get("verbatim_at")
                if need_hook or need_rec:
                    grace = 0
                    if need_hook:
                        grace = max(grace, int(hook_grace_s))
                    if need_rec:
                        grace = max(grace, int(recording_grace_s))
                    row["state"] = STATE_WAIT_REC if need_rec else STATE_WAIT_HOOK
                    row["wait_until"] = now + max(0, grace)
                else:
                    first = self._finalize(row, "ended", now)
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first}

    def sweep(self, inactivity_s: int, hook_expected: bool, hook_grace_s: int, now: float | None = None,
              recording_hold: bool = False, recording_grace_s: int = 180) -> list[dict]:
        """Server-side finalizer.  Returns rows finalized by this sweep."""
        clock = now if now is not None else now_ts()
        done: list[dict] = []
        with exclusive_lock(self.lock_path):
            data = self._read()
            dirty = False
            for row in data["calls"].values():
                state = row.get("state")
                if state == STATE_OPEN and clock - float(row.get("last_activity") or 0) >= inactivity_s:
                    need_hook = bool(hook_expected) and not row.get("hook_received")
                    need_rec = bool(recording_hold) and not row.get("verbatim_at")
                    if need_hook or need_rec:
                        grace = 0
                        if need_hook:
                            grace = max(grace, int(hook_grace_s))
                        if need_rec:
                            grace = max(grace, int(recording_grace_s))
                        row["state"] = STATE_WAIT_REC if need_rec else STATE_WAIT_HOOK
                        row["wait_until"] = clock + max(0, grace)
                        dirty = True
                        continue
                    if self._finalize(row, "timeout", clock):
                        done.append(copy.deepcopy(row))
                    dirty = True
                elif state in (STATE_WAIT_HOOK, STATE_WAIT_REC) and clock >= float(row.get("wait_until") or 0):
                    source = "agent" if row.get("agent_final") else "timeout"
                    if self._finalize(row, source, clock):
                        done.append(copy.deepcopy(row))
                    dirty = True
            if dirty:
                self._write(data)
        return done

    def find_open_by_caller(self, number: str, window_s: int = 3600) -> str:
        """Hook correlation fallback: newest unfinished call from that number."""
        num = clean_number(number)
        if not num:
            return ""
        clock = now_ts()
        best, best_ts = "", 0.0
        with exclusive_lock(self.lock_path):
            for key, row in self._read()["calls"].items():
                f = row.get("fields") or {}
                if num != f.get("caller_id"):  # caller ID only, never the spoken number (I1)
                    continue
                created = float(row.get("created_at") or 0)
                if clock - created > window_s:
                    continue
                if row.get("state") == STATE_FINAL and row.get("hook_received"):
                    continue
                if created > best_ts:
                    best, best_ts = key, created
        return best

    def find_by_conversation(self, conversation_id: Any) -> str:
        """Key of the row already holding this xAI conversation id, or ""."""
        conv = clean_conversation_id(conversation_id)
        if not conv:
            return ""
        with exclusive_lock(self.lock_path):
            for key, row in self._read()["calls"].items():
                if row.get("conversation_id") == conv:
                    return key
        return ""

    def find_by_start(self, caller: Any, call_start: Any, window_s: int = 6 * 3600) -> str:
        """Key of a recent row with this caller ID and platform call start, or ""."""
        num, start = clean_number(caller), clean_call_start(call_start)
        if not (num and start and start_is_precise(start)):
            return ""
        clock = now_ts()
        with exclusive_lock(self.lock_path):
            for key, row in self._read()["calls"].items():
                if row.get("call_start") != start or (row.get("fields") or {}).get("caller_id") != num:
                    continue
                if row.get("conversation_id"):
                    continue  # tied to a conversation id: only that id matches it
                if clock - float(row.get("created_at") or 0) <= window_s:
                    return key
        return ""

    def find_telnyx_caller(self, caller: Any, call_start: Any = "", window_s: int = MATCH_WINDOW_S) -> str:
        """A capture row for this caller whose start is close to the platform start.

        Used when the agent connects after Telnyx already opened the row, and the
        platform start string is not identical to the Telnyx timestamp."""
        num = clean_number(caller)
        if not num:
            return ""
        start_ts = parse_call_start(call_start) if call_start else 0.0
        clock = now_ts()
        best, score = "", 10 ** 12
        with exclusive_lock(self.lock_path):
            for key, row in self._read()["calls"].items():
                if not row.get("telnyx_session") and not row.get("telnyx_recording"):
                    continue
                if row.get("conversation_id"):
                    continue
                if (row.get("fields") or {}).get("caller_id") != num:
                    continue
                anchor = float(row.get("telnyx_start_ts") or 0) or float(row.get("call_start_ts") or 0)
                if start_ts and anchor:
                    delta = abs(anchor - start_ts)
                    if delta > window_s:
                        continue
                elif row.get("state") == STATE_FINAL and row.get("verbatim_at"):
                    continue
                elif clock - float(row.get("created_at") or 0) > max(window_s, 900):
                    continue
                else:
                    delta = clock - float(row.get("created_at") or clock)
                if delta < score:
                    best, score = key, delta
        return best

    def _match_capture(self, calls: dict, info: dict, now: float) -> str:
        rec = str(info.get("recording_id") or "")
        sess = str(info.get("session") or "")
        caller = clean_number(info.get("caller"))
        start = float(info.get("start_ts") or 0)
        for key, row in calls.items():
            if rec and row.get("telnyx_recording") == rec:
                return key
        for key, row in calls.items():
            if sess and row.get("telnyx_session") == sess:
                return key
        if not caller:
            return ""
        best, score = "", 10 ** 12
        for key, row in calls.items():
            linked = str(row.get("telnyx_session") or "")
            if linked and sess and linked != sess:
                continue
            if linked and not sess:
                continue
            if (row.get("fields") or {}).get("caller_id") != caller:
                continue
            if row.get("state") == STATE_FINAL and row.get("verbatim_at") and row.get("telnyx_recording"):
                continue
            anchor = (float(row.get("call_start_ts") or 0) or float(row.get("telnyx_start_ts") or 0)
                      or float(row.get("created_at") or 0))
            if start and anchor and abs(anchor - start) > MATCH_WINDOW_S:
                continue
            delta = abs(anchor - start) if start and anchor else abs(now - float(row.get("created_at") or now))
            if delta < score:
                best, score = key, delta
        return best

    def apply_capture(self, info: Any) -> dict:
        """Attach one Telnyx capture to exactly one call row.  Finalizes once.

        ``info`` carries ids, caller, times, duration, the verbatim transcript
        (or the no-speech notice), and the private audio file name.
        """
        if not isinstance(info, dict):
            return {"row": None, "notify": False, "late": False, "changed": False, "duplicate": False}
        recording_id = str(info.get("recording_id") or "")
        session = str(info.get("session") or "")
        if recording_id and not re.fullmatch(r"^[A-Za-z0-9_.:-]{1,128}$", recording_id):
            recording_id = ""
        if session and not re.fullmatch(r"^[A-Za-z0-9_.:-]{1,128}$", session):
            session = ""
        if not recording_id and not session:
            return {"row": None, "notify": False, "late": False, "changed": False, "duplicate": False}
        text = clean_transcript(info.get("transcript"))
        caller = clean_number(info.get("caller"))
        start_ts = float(info.get("start_ts") or 0)
        end_ts = float(info.get("end_ts") or 0)
        try:
            duration = max(0, int(info.get("duration_s") or 0))
        except (TypeError, ValueError):
            duration = 0
        audio = str(info.get("file") or "")
        if not re.fullmatch(r"^[a-f0-9]{32}\.(wav|mp3)$", audio):
            audio = ""
        now = now_ts()
        with exclusive_lock(self.lock_path):
            data = self._read()
            self._prune(data, now)
            calls = data["calls"]
            key = self._match_capture(calls, {"recording_id": recording_id, "session": session,
                                              "caller": caller, "start_ts": start_ts}, now)
            if not key:
                seed = session or recording_id
                key = hashlib.sha256(f"telnyx:{seed}".encode()).hexdigest()[:32]
            row = self._row(data, key, now)
            parts = [p for p in (row.get("recording_parts") or []) if isinstance(p, dict)]
            if recording_id and any(p.get("id") == recording_id for p in parts):
                self._write(data)
                return {"row": copy.deepcopy(row), "notify": False, "late": row["state"] == STATE_FINAL,
                        "changed": False, "duplicate": True}
            changed = False
            if session and not row.get("telnyx_session"):
                row["telnyx_session"] = session
                changed = True
            leg = str(info.get("leg") or "")
            control = str(info.get("control") or "")
            if leg and re.fullmatch(r"^[A-Za-z0-9_.:-]{1,128}$", leg) and not row.get("telnyx_leg"):
                row["telnyx_leg"] = leg
                changed = True
            if control and re.fullmatch(r"^[A-Za-z0-9_.:-]{1,128}$", control) and not row.get("telnyx_control"):
                row["telnyx_control"] = control
                changed = True
            if recording_id and not row.get("telnyx_recording"):
                row["telnyx_recording"] = recording_id
                changed = True
            known = list(row.get("telnyx_recordings") or [])
            if recording_id and recording_id not in known:
                row["telnyx_recordings"] = (known + [recording_id])[-8:]
                changed = True
            if caller and not row["fields"].get("caller_id"):
                row["fields"]["caller_id"] = caller
                changed = True
            cnam = contacts_sync.clean_name(info.get("cnam") or "")
            if contacts_sync.name_is_usable(cnam) and not row["fields"].get("cnam"):
                row["fields"]["cnam"] = cnam
                changed = True
            if start_ts and not row.get("telnyx_start_ts"):
                row["telnyx_start_ts"] = start_ts
                changed = True
                if not row.get("call_start"):
                    row["call_start"] = datetime.fromtimestamp(start_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    row["call_start_ts"] = start_ts
            if end_ts:
                row["telnyx_end_ts"] = end_ts
            if duration and not row.get("duration_s"):
                row["duration_s"] = duration
                changed = True
            if audio and not row.get("recording_file"):
                row["recording_file"] = audio
                changed = True
            failed = bool(info.get("failed"))
            if failed:
                code = re.sub(r"[^a-z0-9_-]", "", str(info.get("error_code") or "failed").lower())[:40] or "failed"
                row["capture_error"] = code
                if not parts:
                    row["capture_failed"] = True
                # Not a content change.  A failure after the email must not send
                # a "details added" push, even if ids were filled in above.
                changed = False
            elif text:
                parts.append({"id": recording_id or session, "start": start_ts, "text": text})
                parts.sort(key=lambda p: (float(p.get("start") or 0), str(p.get("id") or "")))
                row["recording_parts"] = parts[-8:]
                row["transcript"] = clean_transcript("\n".join(p["text"] for p in row["recording_parts"] if p.get("text")))
                row["verbatim_at"] = now
                row["capture_failed"] = False
                changed = True
            late = row["state"] == STATE_FINAL
            first = False
            ready = bool(row.get("verbatim_at")) or bool(row.get("capture_failed"))
            if not late and ready and not info.get("siblings_open"):
                source = "agent" if row.get("agent_final") else "recording"
                first = self._finalize(row, source, now)
            elif late and changed:
                row["late_updates"] = int(row.get("late_updates") or 0) + 1
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first, "late": late, "changed": changed, "duplicate": False}
