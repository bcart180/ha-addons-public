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
import json
import pathlib
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import call_summary
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
MAX_ROWS = 500
KEEP_FINAL_S = 30 * 24 * 3600

STATE_OPEN = "open"
STATE_WAIT_HOOK = "awaiting_hook"
STATE_FINAL = "final"


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
    def touch(self, key: str, fields: Any = None, caller_hint: str = "") -> dict:
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
            if not late:
                row["last_activity"] = now
            elif changed:
                row["late_updates"] = int(row.get("late_updates") or 0) + 1
            self._write(data)
            return {"row": copy.deepcopy(row), "late": late, "changed": changed}

    def agent_final(self, key: str, kind: str, fields: Any, caller_hint: str = "", hook_expected: bool = False,
                    hook_grace_s: int = 120) -> dict:
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
            first = False
            if late:
                if changed:
                    row["late_updates"] = int(row.get("late_updates") or 0) + 1
            else:
                row["agent_final"] = True
                row["final_kind"] = kind
                row["last_activity"] = now
                if row["hook_received"] or not hook_expected:
                    first = self._finalize(row, "agent", now)
                else:
                    row["state"] = STATE_WAIT_HOOK
                    row["wait_until"] = now + max(0, int(hook_grace_s))
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first, "late": late, "changed": changed}

    def hook(self, key: str, hook_id: str, transcript: Any, caller_hint: str = "", ended_at: float | None = None) -> dict:
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
            if text and len(text) > len(row.get("transcript") or ""):
                row["transcript"] = text
                changed = True
            late = row["state"] == STATE_FINAL
            row["hook_received"] = True
            first = False
            if not late:
                first = self._finalize(row, "hook", now)
            elif changed and not duplicate:
                row["late_updates"] = int(row.get("late_updates") or 0) + 1
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first, "late": late, "duplicate": duplicate,
                    "changed": changed}

    def ended(self, key: str, hook_expected: bool = False, hook_grace_s: int = 120) -> dict:
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
                if hook_expected and not row["hook_received"]:
                    row["state"] = STATE_WAIT_HOOK
                    row["wait_until"] = now + max(0, int(hook_grace_s))
                else:
                    first = self._finalize(row, "ended", now)
            self._write(data)
            return {"row": copy.deepcopy(row), "notify": first}

    def sweep(self, inactivity_s: int, hook_expected: bool, hook_grace_s: int, now: float | None = None) -> list[dict]:
        """Server-side finalizer.  Returns rows finalized by this sweep."""
        clock = now if now is not None else now_ts()
        done: list[dict] = []
        with exclusive_lock(self.lock_path):
            data = self._read()
            dirty = False
            for row in data["calls"].values():
                state = row.get("state")
                if state == STATE_OPEN and clock - float(row.get("last_activity") or 0) >= inactivity_s:
                    if hook_expected and not row.get("hook_received"):
                        row["state"] = STATE_WAIT_HOOK
                        row["wait_until"] = clock + max(0, int(hook_grace_s))
                        dirty = True
                        continue
                    if self._finalize(row, "timeout", clock):
                        done.append(copy.deepcopy(row))
                    dirty = True
                elif state == STATE_WAIT_HOOK and clock >= float(row.get("wait_until") or 0):
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
