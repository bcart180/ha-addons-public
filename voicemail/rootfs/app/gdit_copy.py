"""Optional redacted GDIT work-hours copy of a CallCatch notice (0.8.6+).

Off unless ``gdit_work_copy`` is on.  A copy is considered only after the
Proton notice (sent, or would-send while dry_run is on), only when
``binary_sensor.bryson_at_work`` is explicitly ``on``, and never for VA,
medical, or financial callers.  The body is a redacted stub: contact name or
"A caller", the time in ET, and a line pointing at Proton.  Logs are reason
codes only; names and numbers are never logged.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

WORK_SENSOR = "binary_sensor.bryson_at_work"
# Single source for the pinned work recipient.  Do not duplicate this value
# in docs, changelog, or other modules.
GDIT_RECIPIENT = "bryson.carter@gdit.com"
GDIT_STALE_S = 15 * 60

# Reason codes.  Logs and tests only ever carry one of these.
REASONS = frozenset({
    "ok",
    "disabled",
    "no_proton",
    "work_unknown",
    "not_at_work",
    "sensitive",
    "dry_run",
    "stale",
})

# Phrase list (documented in DOCS).  After NFKC, casefold, punctuation
# collapse, and joining of single-letter tokens, these are substring matches.
# The matcher fails toward skipping: look-alikes and spaced spellings count.
SENSITIVE_PHRASES = (
    "veterans affairs",
    "veteran's affairs",
    "veterans administration",
    "veterans health",
    "the va",
    "va hospital",
    "va clinic",
    "va medical",
    "va appointment",
    "va health",
    "va benefits",
    "va loan",
    "va claim",
    "va office",
    "urgent care",
    "emergency room",
    "medical center",
    "medical centre",
    "medical group",
    "health system",
    "nursing home",
    "lab results",
    "internal revenue",
    "credit union",
    "credit card",
    "navy federal",
    "walter reed",
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
# Word-bounded tokens on the same normalized haystack.  Short or everyday
# terms (va, bank, doctor, claim) are included on purpose: prefer skip over
# sending a work copy for a sensitive caller.
SENSITIVE_WORDS = (
    "va",
    "vamc",
    "claim",
    "benefits",
    "dr",
    "doctor",
    "md",
    "dds",
    "therapist",
    "therapy",
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
    "medstar",
    "medicaid",
    "medicare",
    "prescription",
    "insurance",
    "tricare",
    "usaa",
    "bank",
    "cvs",
    "walgreens",
    "kaiser",
    "citi",
    "discover",
    "paypal",
    "brokerage",
    "mortgage",
    "loan",
    "newrez",
    "fidelity",
    "vanguard",
    "turbotax",
    "irs",
    "labcorp",
)
# Compact stems: longer needles matched on the compact haystack so
# "oncologist" hits "oncolog".  Short or ambiguous tokens are listed in
# SENSITIVE_BOUND_WORDS and stay word-bounded only.
SENSITIVE_STEMS = (
    "oncolog",
    "cardiolog",
    "dermatolog",
    "psychiatr",
    "psycholog",
    "counsel",
    "chiropract",
    "optom",
    "orthop",
    "urolog",
    "obgyn",
    "eyecare",
    "schwab",
    "venmo",
    "truist",
    "synchrony",
    "equifax",
    "experian",
    "transunion",
    "accountant",
)
# Word-bounded only.  Compact substring would over-match short stems
# ("ally" in "valley", "chase" in "purchase", "tax" is already < 4).
SENSITIVE_BOUND_WORDS = (
    "nurse",
    "nurses",
    "chase",
    "amex",
    "pnc",
    "ally",
    "sofi",
    "cpa",
    "tax",
    "credit",
)

_WORD = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_NON_ALNUM = re.compile(r"[^\w]+", re.UNICODE)
# Common look-alikes that NFKC does not fold into Latin letters.
_HOMOGLYPHS = str.maketrans({
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "Ј": "J", "Ү": "Y",
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ј": "j", "ѕ": "s",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "η": "n",
    # Latin small capitals (NFKC leaves these alone).
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g",
    "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n",
    "ᴏ": "o", "ᴘ": "p", "ꞯ": "q", "ʀ": "r", "ꜱ": "s", "ᴛ": "t", "ᴜ": "u",
    "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})


def normalize_match_text(raw: Any) -> str:
    """NFKC, look-alike fold, casefold, strip controls, collapse punctuation and space.

    Adjacent single-letter tokens are joined so spaced spellings ("V A", "D r")
    match the same needles as the compact form.
    """
    text = unicodedata.normalize("NFKC", str(raw or ""))
    text = text.translate(_HOMOGLYPHS)
    text = _CTRL.sub(" ", text).casefold()
    text = _NON_ALNUM.sub(" ", text)
    text = _WORD.sub(" ", text).strip()
    if not text:
        return ""
    parts = text.split(" ")
    out: list[str] = []
    buf: list[str] = []
    for part in parts:
        if len(part) == 1:
            buf.append(part)
            continue
        if buf:
            out.append("".join(buf))
            buf = []
        out.append(part)
    if buf:
        out.append("".join(buf))
    return " ".join(out)


def _compact(text: str) -> str:
    return text.replace(" ", "")


def _haystack(raw: Any) -> str:
    return normalize_match_text(raw)


def _label_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(v or "") for v in value)
    return str(value or "")


def match_text(row: dict | None) -> str:
    """Resolved contact name and org, plus summary and transcript."""
    row = row or {}
    f = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    parts = [
        f.get("caller_name") or "",
        f.get("org") or "",
        f.get("caller_label") or "",
        f.get("label") or "",
        _label_text(f.get("labels")),
        row.get("summary") or "",
        row.get("transcript") or "",
    ]
    return normalize_match_text(" ".join(parts))


def is_sensitive(row: dict | None) -> bool:
    """True when the resolved name, org, summary, or transcript looks VA, medical, or financial."""
    text = match_text(row)
    if not text:
        return False
    compact = _compact(text)
    for phrase in SENSITIVE_PHRASES:
        needle = normalize_match_text(phrase)
        if not needle:
            continue
        if needle in text:
            return True
        packed = _compact(needle)
        if len(packed) >= 4 and packed in compact:
            return True
    for word in SENSITIVE_WORDS:
        needle = normalize_match_text(word)
        if not needle:
            continue
        if re.search(r"\b" + re.escape(needle) + r"\b", text):
            return True
        packed = _compact(needle)
        if len(packed) >= 4 and packed in compact:
            return True
    for stem in SENSITIVE_STEMS:
        needle = normalize_match_text(stem)
        packed = _compact(needle)
        if packed and packed in compact:
            return True
    for word in SENSITIVE_BOUND_WORDS:
        needle = normalize_match_text(word)
        if needle and re.search(r"\b" + re.escape(needle) + r"\b", text):
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


def job_is_stale(job: dict | None, now: float | None = None) -> bool:
    """True when a GDIT job is older than GDIT_STALE_S (about 15 minutes)."""
    import time

    created = float((job or {}).get("created_at") or 0)
    clock = time.time() if now is None else float(now)
    if created <= 0:
        return True
    return (clock - created) > GDIT_STALE_S
