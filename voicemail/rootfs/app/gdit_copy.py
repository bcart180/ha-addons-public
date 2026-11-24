"""Optional redacted GDIT work-hours copy of a CallCatch notice (0.8.6).

Off unless ``gdit_work_copy`` is on.  A copy is considered only after the
Proton notice (sent, or would-send while dry_run is on), only when
``binary_sensor.bryson_at_work`` is explicitly ``on``, and never for VA,
medical, or financial callers.  The body is a redacted stub: contact name or
"A caller", the time in ET, and a line pointing at Proton.  Logs are reason
codes only; names and numbers are never logged.
"""
from __future__ import annotations

import re
from typing import Any

WORK_SENSOR = "binary_sensor.bryson_at_work"
GDIT_RECIPIENT = "bryson.carter@gdit.com"

# Reason codes.  Logs and tests only ever carry one of these.
REASONS = frozenset({
    "ok",
    "disabled",
    "no_proton",
    "work_unknown",
    "not_at_work",
    "sensitive",
    "dry_run",
})

# Conservative phrase list (documented in DOCS).  Multi-word phrases are
# matched as substrings on a lowercased, whitespace-collapsed haystack.
# Single tokens are word-bounded so short or ambiguous terms do not fire
# alone (for example bare "va" or "bank" or "doctor" are intentionally
# omitted).
SENSITIVE_PHRASES = (
    "veterans affairs",
    "veteran's affairs",
    "veterans administration",
    "veterans health",
    "va hospital",
    "va clinic",
    "va medical",
    "va appointment",
    "va health",
    "urgent care",
    "emergency room",
    "medical center",
    "medical centre",
    "medical group",
    "health system",
    "nursing home",
    "internal revenue",
    "credit union",
    "credit card",
    "bank of america",
    "wells fargo",
    "chase bank",
    "capital one",
    "american express",
    "charles schwab",
    "h&r block",
    "hr block",
    "loan servicer",
    "collections agency",
    "payday loan",
    "roth ira",
    "401(k)",
    "401k",
    "social security",
    "quest diagnostics",
)
SENSITIVE_WORDS = (
    "vamc",
    "hospital",
    "clinic",
    "pharmacy",
    "pharmacist",
    "physician",
    "dentist",
    "dental",
    "dermatology",
    "oncology",
    "pediatric",
    "pediatrics",
    "cardiology",
    "radiology",
    "hospice",
    "medicaid",
    "medicare",
    "prescription",
    "brokerage",
    "mortgage",
    "fidelity",
    "vanguard",
    "turbotax",
    "irs",
    "labcorp",
)

_WORD = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def _haystack(raw: Any) -> str:
    return _WORD.sub(" ", _CTRL.sub(" ", str(raw or "")).lower()).strip()


def _label_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v or "") for v in value)
    return str(value or "")


def match_text(row: dict | None) -> str:
    """Name, label, summary, and transcript only.  Used for the skip list."""
    row = row or {}
    f = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    parts = [
        f.get("caller_name") or "",
        f.get("caller_label") or "",
        f.get("label") or "",
        _label_text(f.get("labels")),
        f.get("org") or "",
        row.get("summary") or "",
        row.get("transcript") or "",
    ]
    return _haystack(" ".join(parts))


def is_sensitive(row: dict | None) -> bool:
    """True when the contact name/label, summary, or transcript looks VA, medical, or financial."""
    text = match_text(row)
    if not text:
        return False
    for phrase in SENSITIVE_PHRASES:
        if phrase in text:
            return True
    for word in SENSITIVE_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", text):
            return True
    return False


def work_gate(enabled: bool, state: str | None) -> str:
    """Return a reason code.  ``ok`` means the work sensor allows a copy.

    Only an explicit ``on`` from ``binary_sensor.bryson_at_work`` passes.
    Missing, unknown, unavailable, or an unreachable HA read is fail-closed.
    """
    if not enabled:
        return "disabled"
    if state == "on":
        return "ok"
    if state == "off":
        return "not_at_work"
    return "work_unknown"


def decide(enabled: bool, proton: str, state: str | None, row: dict | None) -> str:
    """Pick the single reason code for a GDIT copy attempt."""
    if proton not in ("sent", "would-send"):
        return "no_proton"
    gate = work_gate(enabled, state)
    if gate != "ok":
        return gate
    if is_sensitive(row):
        return "sensitive"
    if proton == "would-send":
        return "dry_run"
    return "ok"
