"""MCP tool definitions and argument validation for CallCatch Line.

Only four tools exist.  None reads stored messages, transcripts, contacts,
or any Home Assistant entity.  Write tools append to CallCatch's own call
record.  Caller text is untrusted data: it is validated here and cleaned
again in CallCatch, and it is never echoed back.
"""
from __future__ import annotations

import re
from typing import Any

URGENCY = ["low", "normal", "urgent"]
_STR = {
    "caller_name": 60,
    "callback_number": 20,
    "reason": 280,
    "best_time": 80,
    "preferred_window": 80,
    "notes": 600,
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUM_RE = re.compile(r"^[0-9+()\-.\s]{7,20}$")


def _s(desc: str, n: int) -> dict:
    return {"type": "string", "maxLength": n, "description": desc}


_FIELDS = {
    "caller_name": _s("Caller's name as they said it.", 60),
    "callback_number": _s("Number to call back, digits only or US format.  Leave out to use the caller ID.", 20),
    "reason": _s("Short reason for the call in the caller's words.", 280),
    "urgency": {"type": "string", "enum": URGENCY, "description": "low, normal, or urgent (urgent only if the caller says so)."},
    "best_time": _s("When the caller says is best to reach them.", 80),
    "notes": _s("Any other detail the caller gave.  No guesses.", 600),
}

TOOLS = [
    {
        "name": "update_call",
        "description": (
            "Save what you have learned so far on this call.  Call it as soon as you know the caller's "
            "name or reason, and again whenever you learn more.  It protects the message if the call drops.  "
            "It never sends anything to the caller and never reads other calls."
        ),
        "inputSchema": {"type": "object", "properties": dict(_FIELDS), "additionalProperties": False},
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "take_message",
        "description": (
            "Save the caller's message for Bryson.  Call once, when the caller is done.  Bryson gets it right away.  "
            "Read back the reference it returns.  Do not promise a callback time."
        ),
        "inputSchema": {
            "type": "object",
            "properties": dict(_FIELDS),
            "required": ["reason"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "request_callback",
        "description": (
            "Save the message and ask Bryson to call the caller back.  Use instead of take_message when the "
            "caller wants a call back.  Nothing is dialed and no time is booked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_FIELDS,
                "preferred_window": _s("When the caller would like the call back, in their words.", 80),
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "get_free_busy",
        "description": (
            "Bryson's busy times only (weekdays 8 AM to 6 PM Eastern).  No event details exist here.  "
            "Use it to suggest a general window.  Never book or confirm a meeting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "YYYY-MM-DD, today or later.  Default today."},
                "days": {"type": "integer", "minimum": 1, "maximum": 14, "description": "Days to cover, 1-14.  Default 5."},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}
_ALLOWED = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}
_REQUIRED = {t["name"]: set(t["inputSchema"].get("required", [])) for t in TOOLS}

INSTRUCTIONS = (
    "CallCatch tools for Bryson's phone line.  Call update_call early and as details come in.  "
    "Finish with take_message or request_callback.  get_free_busy gives busy times only.  "
    "These tools cannot read past messages, contacts, or anything else in the house."
)


def validate(name: str, args: Any) -> tuple[dict | None, str]:
    """Return (clean_args, "") or (None, error_word)."""
    if name not in TOOL_NAMES:
        return None, "unknown-tool"
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, "bad-arguments"
    extra = set(args) - _ALLOWED[name]
    if extra:
        return None, "unknown-argument"
    missing = [k for k in _REQUIRED[name] if not str(args.get(k) or "").strip()]
    if missing:
        return None, "missing-" + missing[0]
    out: dict = {}
    for k, v in args.items():
        if v is None:
            continue
        if k == "days":
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                return None, "bad-days"
            try:
                n = int(v)
            except (TypeError, ValueError):
                return None, "bad-days"
            if not 1 <= n <= 14:
                return None, "bad-days"
            out[k] = n
            continue
        if not isinstance(v, str):
            return None, "bad-" + k
        if k == "start_date":
            if v and not _DATE_RE.fullmatch(v):
                return None, "bad-start_date"
            out[k] = v
            continue
        if k == "urgency":
            if v not in URGENCY:
                return None, "bad-urgency"
            out[k] = v
            continue
        limit = _STR.get(k)
        if limit is None:
            return None, "unknown-argument"
        if len(v) > limit:
            return None, "too-long-" + k
        if k == "callback_number" and v and not _NUM_RE.fullmatch(v):
            return None, "bad-callback_number"
        out[k] = v
    return out, ""
