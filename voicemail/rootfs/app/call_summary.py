"""Post-call structured summary: caller name, reason, callback number, urgency.

Two sources, both keyless inside this add-on:

* ``voice``: CallCatch Voice may send a ``summary`` object with the ingest
  payload (its own xAI session already produced it). It is never trusted as is:
  every field is length-capped and cleaned, urgency must be one of three
  values, and a callback number is kept only when its digits are the caller's
  number or are actually spoken in the transcript (no invented numbers).
* ``local``: a deterministic extractor over the transcript, used when no
  summary was sent and for rows saved before 0.3.7. No network, no model.

Nothing here is specific to one household. Numbers shown outside the
admin-only panel (sensor attributes, push notifications) go through
``mask_number`` / ``mask_phone_runs``, the same ``•••1234`` masking the
pyscript sensor and its notifications already use.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

URGENCY = ("low", "normal", "urgent")
NAME_MAX = 60
REASON_MAX = 160
FALLBACK_MAX = 140

_SPEAKER = re.compile(r"^\s*(house|assistant|agent|caller)\s*:\s*(.*)$", re.I)
# Between digits: up to 8 non-alphanumeric characters (any dash, minus,
# bullet, dot, comma, quotes, spaces...), or one short letter token such as
# "x", "and", "then" or markup residue like "b/b". 7+ digits make a phone run.
# Whitespace is collapsed and digits are normalized to ASCII first.
_SEP = r"(?:[\W_]{0,8}|(?:[\W_]{0,4}[^\W\d_]{1,4}){1,2}[\W_]{0,4})"
PHONE_RUN_RE = re.compile(r"\+?\d(?:" + _SEP + r"\d){6,}")
# Callback detection stays strict (spoken numbers with plain separators).
SPOKEN_RUN_RE = re.compile(r"\+?\d(?:[^\w]{0,3}\d|_{1,3}\d){6,}(?:\s*(?:x|ext\.?)\s*\d+)?", re.I)
# Number-like tokens (digits mixed with O/o/I/l and punctuation only).
_LOOKALIKE_TOKEN = re.compile(r"(?<![^\W\d_])[\dOoIl().\-+/]*\d[\dOoIl().\-+/]*(?![^\W\d_])")
EXT_SPLIT_RE = re.compile(r"\s*(?:x|ext\.?)\s*\d+$", re.I)

_NAME_PATTERNS = (
    re.compile(r"\bmy name is\s+([A-Z][\w'’.-]*(?:\s+(?:[A-Z][\w'’.-]*|&|and|of)){0,4})"),
    re.compile(r"\b(?:this is|it'?s|it is)\s+([A-Z][\w'’.-]*(?:\s+(?:[A-Z][\w'’.-]*|&|and|of)){0,4})(?=\s+(?:from|with|calling|at|here)\b|[,.!?]|$)"),
    re.compile(r"\b([A-Z][\w'’.-]*(?:\s+[A-Z][\w'’.-]*){0,2})\s+(?:here|calling)\b"),
)
_NOT_NAMES = {
    "i", "hi", "hey", "hello", "yes", "yeah", "no", "ok", "okay", "just", "so", "um", "uh",
    "the", "a", "an", "this", "that", "me", "us", "we", "you", "your", "about", "regarding",
    "calling", "a call", "sorry", "thanks", "thank", "good", "morning", "afternoon", "evening",
}
_REASON_PATTERNS = (
    re.compile(r"\b(?:calling|call|reaching out|following up|getting back to you)\s+(?:you\s+)?(?:about|regarding|re|on|to|because|for)\b\s*(.+)", re.I),
    re.compile(r"\b(?:wanted|need|needed|want|would like|i'?d like)\s+to\s+(.+)", re.I),
    re.compile(r"\b(?:this is (?:about|regarding))\s+(.+)", re.I),
)
_URGENT = re.compile(
    r"\b(urgent|urgently|emergency|asap|as soon as possible|right away|immediately|"
    r"right now|time[- ]sensitive|today if you can|before (?:tonight|end of day|5)|"
    r"leak(?:ing)?|flood(?:ing)?|fire|smoke|gas smell|smell gas|ambulance|hospital|"
    r"accident|police|locked out|no heat|no power|final notice)\b",
    re.I,
)
_LOW = re.compile(
    r"\b(no rush|no hurry|whenever you (?:get a chance|can)|no need to call(?: me)? back|"
    r"just (?:checking in|a reminder|wanted to say|letting you know|a heads[- ]up)|"
    r"not urgent|nothing urgent|fyi|at your convenience)\b",
    re.I,
)


_DROP_CATS = {"Cf", "Mn", "Me"}
# Invisible fillers that are not Cf/Mn: Hangul fillers, braille blank.
_FILLERS = {"\u115f", "\u1160", "\u3164", "\u2800", "\uffa0"}


def _norm_char(ch: str) -> str:
    if ch.isdigit() and not ch.isascii():
        try:
            return str(unicodedata.decimal(ch))
        except ValueError:
            return str(unicodedata.digit(ch, "")) or ch
    return ch


def strip_format(text: Any) -> str:
    """Drop Unicode Cf (zero-width, bidi, soft hyphen, BOM), Mn/Me combining
    marks (incl. U+034F and variation selectors) and invisible fillers, and
    map every Unicode digit (fullwidth, Arabic-Indic, ...) to ASCII. Runs
    before any masking so nothing can hide or disguise a number."""
    return "".join(
        _norm_char(ch)
        for ch in str(text or "")
        if ch not in _FILLERS and unicodedata.category(ch) not in _DROP_CATS
    )


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\((?:[^()]|\([^()]*\))*\)")
_URL = re.compile(r"\b(?:https?|ftp|javascript|data|file):\S*|\bwww\.\S+", re.I)
_MARKUP = re.compile(r"[*_`~#|\[\]<>]")


def plain_text(text: Any) -> str:
    """Neutralize markup: Markdown links keep their label, URLs become
    "[link]", and emphasis/code/heading/HTML characters are removed."""
    s = strip_format(text)
    s = _MD_LINK.sub(lambda m: m.group(1), s)
    s = _URL.sub("(link)", s)
    s = re.sub(r"&(?:[a-z]+|#\d+);", " ", s, flags=re.I)
    return _MARKUP.sub("", s)


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def mask_number(num: Any) -> str:
    """'+1 (555) 555-0199' -> '•••0199' (same as the pyscript sensor)."""
    s = str(num or "").strip()
    if not s:
        return ""
    d = digits(s)
    if len(d) < 4:
        return s if not d else "•••"
    return "•••" + d[-4:]


def _fix_lookalikes(text: str) -> str:
    """'54O-555-O177' / 'l-555-0177': inside a number-like token, letter O/o
    reads as 0 and I/l as 1. Tokens that also hold other letters are left."""

    def fix(m: re.Match) -> str:
        tok = m.group(0)
        if sum(ch.isdigit() for ch in tok) < 3:
            return tok
        return tok.translate(str.maketrans("OoIl", "0011"))

    return _LOOKALIKE_TOKEN.sub(fix, text)


def mask_phone_runs(text: Any) -> str:
    s = re.sub(r"\s+", " ", strip_format(text))
    s = _fix_lookalikes(s)

    def mask(m: re.Match) -> str:
        run = m.group(0)
        base = EXT_SPLIT_RE.sub("", run)
        # Drop an extension only when what is left is still a whole number.
        return mask_number(base if len(digits(base)) >= 7 else run)

    return PHONE_RUN_RE.sub(mask, s)


def pretty_number(d: str) -> str:
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) == 10:
        return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return d


def _same_number(a: str, b: str) -> bool:
    if len(a) < 7 or len(b) < 7:
        return False
    return a[-10:] == b[-10:] if len(a) >= 10 and len(b) >= 10 else a[-7:] == b[-7:]


# Hangup header the ingest path prepends ("Caller: <number>" line, masked number,
# "When:", "Call ID:", "Source: CallCatch",
# "Duration: 14 seconds" / "Duration_s: 14.2", "TRANSCRIPT"). It is metadata,
# not speech: strip it from the front so reason/preview start with the caller.
_HEADER_SCAN = 1024  # header lives at the very front; never regex the whole body
_HEADER_MAX_TOKENS = 16
# Every quantifier is bounded so a hostile transcript cannot trigger
# super-linear backtracking (ReDoS). Matched with .match() at the front only.
_HEADER_HEAD = re.compile(
    r"(?:[•*·]{2,4}[ \t]{0,4}\d{2,4}\b"
    r"|Source:[ \t]{0,8}[^\s]{1,40}"
    r"|Caller:[ \t]{0,8}(?:\+?[\d().-][\d \t().-]{6,39}|[•*·]{2,4}\d{2,4}|Unknown)[ \t]{0,8}(?=\r?\n)"
    r"|(?:When|Call ID):[^\r\n]{0,120}(?=\r?\n)"
    r"|Duration(?:_s)?:[ \t]{0,8}[0-9.]{1,8}(?:[ \t]{0,4}(?:minutes?|seconds?|s)\b)?"
    r"(?:[ \t]{1,4}[0-9.]{1,8}[ \t]{0,4}(?:minutes?|seconds?|s)\b)?)"
    r"\s{0,8}",
    re.I,
)
_TRANSCRIPT_MARK = re.compile(r"TRANSCRIPT\b:?\s{0,8}", re.I)


def strip_header(text: Any) -> str:
    """Drop the leading hangup header only. Scans the first 1,024 chars, stops
    at the first non-header token, and stops after the TRANSCRIPT marker, so
    anything after the marker (e.g. a spoken "When: ...") is kept."""
    s = str(text or "")
    head, tail = s[:_HEADER_SCAN].lstrip(), s[_HEADER_SCAN:]
    for _ in range(_HEADER_MAX_TOKENS):
        m = _TRANSCRIPT_MARK.match(head)
        if m:
            head = head[m.end():]
            break
        m = _HEADER_HEAD.match(head)
        if not m or not m.end():
            break
        head = head[m.end():]
    return head + tail


def caller_text(transcript: Any) -> str:
    """Caller-side speech. Labelled transcripts keep only Caller lines (and
    unlabelled continuation lines that follow one); unlabelled text is all caller."""
    text = strip_header(transcript)
    lines = [ln for ln in re.split(r"\r\n|\n|\r", text) if ln.strip()]
    if not any(_SPEAKER.match(ln) for ln in lines):
        return " ".join(ln.strip() for ln in lines)
    out, keep = [], False
    for ln in lines:
        m = _SPEAKER.match(ln)
        if m:
            keep = m.group(1).lower() == "caller"
            if keep and m.group(2).strip():
                out.append(m.group(2).strip())
        elif keep:
            out.append(ln.strip())
    return " ".join(out)


def _clean(text: Any, limit: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]", " ", plain_text(text))
    s = re.sub(r"\s+", " ", s).strip().strip("\"'")
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _spoken_numbers(text: str) -> list[str]:
    found = []
    for m in SPOKEN_RUN_RE.finditer(text):
        d = digits(EXT_SPLIT_RE.sub("", m.group(0)))
        if 7 <= len(d) <= 15:
            found.append(d)
    return found


def extract_name(text: str) -> str:
    for pat in _NAME_PATTERNS:
        for m in pat.finditer(text):
            name = m.group(1).strip(" .,'’-")
            # Trim trailing connector words picked up by the greedy group.
            name = re.sub(r"\s+(?:and|of|&)$", "", name)
            if not name or name.lower() in _NOT_NAMES or name.split()[0].lower() in _NOT_NAMES:
                continue
            if any(ch.isdigit() for ch in name):
                continue
            return _clean(name, NAME_MAX)
    return ""


def extract_reason(text: str) -> str:
    sents = _sentences(text)
    for s in sents:
        for pat in _REASON_PATTERNS:
            m = pat.search(s)
            if m and len(m.group(1).strip()) > 3:
                r = m.group(1).strip().rstrip(".!?")
                return _clean(r[:1].upper() + r[1:], REASON_MAX)
    for s in sents:
        low = re.sub(r"^(hi|hey|hello|good (morning|afternoon|evening))\b[\s,!.]*", "", s.lower()).strip()
        if not low:
            continue
        if re.match(r"^(this is|my name is|it'?s|it is)\b", low) and len(low.split()) <= 6:
            continue
        return _clean(s.rstrip(".!?"), REASON_MAX)
    return ""


def extract_urgency(text: str) -> str:
    if _URGENT.search(text):
        return "urgent"
    if _LOW.search(text):
        return "low"
    return "normal"


def _callback(caller_digits: str, transcript_text: str, proposed: str = "") -> str:
    spoken = _spoken_numbers(transcript_text)
    pd = digits(strip_format(proposed))
    if pd:
        if not 7 <= len(pd) <= 15:
            return ""
        if _same_number(pd, caller_digits) or any(_same_number(pd, s) for s in spoken):
            return pd
        return ""  # never show a number nobody said
    for s in spoken:
        if not _same_number(s, caller_digits):
            return s  # a different number the caller asked us to use
    return caller_digits if valid_callback(caller_digits) else ""


def valid_callback(d: str) -> bool:
    return d.isdigit() and 7 <= len(d) <= 15


def extract_summary(transcript: Any, caller: Any = "") -> dict:
    transcript = strip_format(transcript)
    text = caller_text(transcript)
    cd = digits(caller)
    if not text.strip():
        return {
            "name": "", "reason": "", "callback": _callback(cd, ""), "urgency": "normal", "source": "local",
        }
    return {
        "name": extract_name(text),
        "reason": _clean(mask_phone_runs(extract_reason(text)), REASON_MAX),
        "callback": _callback(cd, str(transcript or "")),
        "urgency": extract_urgency(text),
        "source": "local",
    }


def sanitize_summary(raw: Any, transcript: Any, caller: Any = "") -> dict | None:
    """Validate a summary sent by CallCatch Voice. None when unusable."""
    if not isinstance(raw, dict):
        return None
    name = _clean(raw.get("caller_name") or raw.get("name") or "", NAME_MAX)
    if any(ch.isdigit() for ch in name):
        name = ""
    reason = _clean(mask_phone_runs(plain_text(raw.get("reason") or "")), REASON_MAX)
    urgency = str(raw.get("urgency") or "").strip().lower()
    if urgency not in URGENCY:
        urgency = "normal"
    cb = _callback(digits(caller), strip_format(transcript), str(raw.get("callback") or raw.get("callback_number") or ""))
    if not (name or reason):
        return None
    return {"name": name, "reason": reason, "callback": cb, "urgency": urgency, "source": "voice"}


def summarize(body_summary: Any, transcript: Any, caller: Any = "") -> dict:
    return sanitize_summary(body_summary, transcript, caller) or extract_summary(transcript, caller)


def stored_or_extract(item: dict) -> dict:
    """Summary for a stored row: keep a valid saved one, else derive it."""
    saved = item.get("summary")
    transcript = item.get("transcript") or item.get("snippet") or ""
    caller = item.get("caller") or item.get("caller_pretty") or item.get("caller_display") or ""
    if isinstance(saved, dict) and saved.get("urgency") in URGENCY:
        return {
            "name": _clean(saved.get("name"), NAME_MAX),
            "reason": _clean(mask_phone_runs(plain_text(strip_header(saved.get("reason")))), REASON_MAX),
            "callback": cb if valid_callback(cb := digits(saved.get("callback"))) else "",
            "urgency": saved["urgency"],
            "source": saved.get("source") if saved.get("source") in ("voice", "local") else "local",
        }
    return extract_summary(transcript, caller)


def for_display(summary: dict) -> dict:
    """Admin-only panel view: full number, tap-to-call."""
    cb = summary.get("callback") or ""
    return dict(summary, callback_display=pretty_number(cb) if cb else "", callback_tel=f"tel:+{cb}" if len(cb) == 11 else (f"tel:{cb}" if cb else ""))


def for_sensor(summary: dict) -> dict:
    """Shared sensor / push fields: numbers masked like the existing notifications."""
    return {
        "summary_name": _clean(mask_phone_runs(plain_text(summary.get("name") or "")), NAME_MAX),
        "summary_reason": _clean(mask_phone_runs(plain_text(summary.get("reason") or "")), REASON_MAX),
        "summary_callback": mask_number(summary.get("callback") or ""),
        "urgency": summary.get("urgency") or "normal",
    }


def fallback_line(item: dict) -> str:
    """Push/logbook line for rows without a summary: the first transcript line,
    through the same strip_format + plain_text + mask_phone_runs path, so a
    lock-screen alert never shows more than the last 4 digits of a number.
    plain_text runs before masking so removed markup cannot join digit runs."""
    raw = strip_header(strip_format(item.get("transcript") or item.get("snippet") or ""))
    line = next((ln for ln in re.split(r"\r\n|\n|\r", raw) if ln.strip()), "")
    return _clean(mask_phone_runs(plain_text(line)), FALLBACK_MAX)


def caller_label(item: dict) -> str:
    """Push title: a contact name stays, any number in it shows last 4 only."""
    title = _clean(mask_phone_runs(plain_text(item.get("caller_title") or "")), NAME_MAX)
    if title and not set(title) <= set("•0123456789+()-. "):
        return title
    num = item.get("caller") or item.get("caller_pretty") or ""
    return mask_number(strip_format(num)) if digits(strip_format(num)) else ""
