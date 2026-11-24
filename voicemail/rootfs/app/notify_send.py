"""Senders for the notification outbox (0.7.0).

Deterministic only: no LLM, no routine, no drain.  Email goes straight to the
AgentMail REST API from an AgentMail inbox to ONE recipient whose domain is
pinned in code (Proton).  Gmail is refused.  The rendered subject, text and
HTML are sent verbatim; the category and labels are fixed.  URLs, API keys and
response bodies are never logged or returned; senders return a short code.
"""
from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from aiohttp import ClientTimeout

BLOCKED_MAIL_DOMAINS = ("gmail.com", "googlemail.com")
# Pinned recipient domains: Bryson's Proton.  Exact match, no subdomains.
RECIPIENT_DOMAINS = ("proton.me", "protonmail.com")
# Separate pin for the optional GDIT work-hours copy.  Exact address only;
# subdomains are not accepted.  This does not change the Proton pin above.
GDIT_DOMAINS = ("gdit.com",)
GDIT_RECIPIENT = "bryson.carter@gdit.com"
CATEGORY_GDIT = "callcatch_gdit_notice"
LABELS_GDIT = ("callcatch", CATEGORY_GDIT)
# Sender must be a DEDICATED CallCatch-only AgentMail inbox:
# an AgentMail-hosted inbox whose local part starts with "callcatch", so a shared bot
# inbox can never be configured as the From address.  Keeping it out of reach of other
# agents' AgentMail connectors is a go-live step (DOCS "Dedicated sender inbox").
SENDER_RE = re.compile(r"^callcatch[a-z0-9._\-]{0,55}@agentmail\.to$")
AGENTMAIL_API = "https://api.agentmail.to/v0/inboxes/{inbox}/messages/send"
CATEGORY = "callcatch_phone_message"
LABELS = ("callcatch", CATEGORY)
EMAIL_RE = re.compile(r"^[A-Za-z0-9._+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
# AgentMail send dedupe (R3-L2): Idempotency-Key header, 1-256 of A-Z a-z 0-9 - . _ ~.
# A retry with the same key and body returns the first message and sends nothing.
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~\-]{1,256}$")
REF_RE = re.compile(r"^CC-[A-Z0-9]{5}$")
NOTIFY_RE = re.compile(r"^mobile_app_[a-z0-9_]{1,80}$")
TIMEOUT = ClientTimeout(total=20)
SUBJECT_MAX = 200
TEXT_MAX = 120_000
HTML_MAX = 400_000


def mail_allowed(addr: Any) -> bool:
    """Well-formed and not Gmail."""
    text = str(addr or "").strip()
    if not EMAIL_RE.fullmatch(text):
        return False
    domain = text.rsplit("@", 1)[1].lower()
    return not any(domain == d or domain.endswith("." + d) for d in BLOCKED_MAIL_DOMAINS)


def recipient_allowed(addr: Any) -> bool:
    """The single recipient: not Gmail, and on a pinned Proton domain."""
    text = str(addr or "").strip()
    if not mail_allowed(text):
        return False
    return text.rsplit("@", 1)[1].lower() in RECIPIENT_DOMAINS


def parse_recipient(raw: Any) -> str:
    """Exactly one address.  A list, a non-Proton or a Gmail address gives ""."""
    text = str(raw or "").strip()
    if not text or any(c in text for c in ",; \t\r\n"):
        return ""
    return text if recipient_allowed(text) else ""


def gdit_recipient_allowed(addr: Any) -> bool:
    """Pinned work address on gdit.com only.  Does not loosen the Proton pin."""
    text = str(addr or "").strip()
    if not EMAIL_RE.fullmatch(text):
        return False
    domain = text.rsplit("@", 1)[1].lower()
    if domain not in GDIT_DOMAINS:
        return False
    return text.lower() == GDIT_RECIPIENT


def parse_gdit_recipient(raw: Any) -> str:
    """Exactly bryson.carter@gdit.com.  Any other address, list, or domain gives ""."""
    text = str(raw or "").strip()
    if not text or any(c in text for c in ",; \t\r\n"):
        return ""
    return text.lower() if gdit_recipient_allowed(text) else ""


def sender_allowed(inbox: Any) -> bool:
    text = str(inbox or "").strip().lower()
    return bool(SENDER_RE.fullmatch(text)) and mail_allowed(text)


def idempotency_key(job: dict) -> str:
    """``callcatch-<ref>``: one key per phone message, stable across retries."""
    ref = str(job.get("ref") or "")
    if not REF_RE.fullmatch(ref):
        jid = str(job.get("id") or "")
        ref = jid.split(":", 1)[1] if jid.startswith("email:") else ""
    key = f"callcatch-{ref}" if REF_RE.fullmatch(ref) else ""
    return key if IDEMPOTENCY_RE.fullmatch(key) else ""


def gdit_idempotency_key(job: dict) -> str:
    """``callcatch-gdit-<ref>``: distinct from the Proton notice key."""
    ref = str(job.get("ref") or "")
    if not REF_RE.fullmatch(ref):
        jid = str(job.get("id") or "")
        ref = jid.split(":", 1)[1] if jid.startswith("gdit:") else ""
    key = f"callcatch-gdit-{ref}" if REF_RE.fullmatch(ref) else ""
    return key if IDEMPOTENCY_RE.fullmatch(key) else ""


# R4-L1: AgentMail answers 409 for two different Idempotency-Key cases.  "Still in
# flight" can succeed on retry; "key already used for a different request" never can.
# Error bodies carry a stable ``code`` (and a legacy ``name``).  These mean key reuse
# and park the job at once.  AgentMail's generic send ``conflict`` code covers both
# cases, so it stays retryable and the outbox caps it (notify_outbox.MAX_CONFLICT_RETRIES).
KEY_REUSE_CODES = frozenset({"idempotency_conflict", "idempotency_key_reused", "idempotency_key_mismatch"})
KEY_REUSE_NAMES = frozenset({"idempotencyconflicterror", "idempotencykeyreusederror"})
ERROR_BODY_MAX = 4096


def _classify(status: int) -> tuple[bool, str, bool]:
    if 200 <= status < 300:
        return True, "sent", False
    # 409 here is the retryable kind; agentmail_sender reads the body first (R4-L1).
    if status in (408, 409, 425, 429) or status >= 500 or status == 0:
        return False, f"http-{status}", True
    return False, f"http-{status}", False


def conflict_is_key_reuse(raw: Any) -> bool:
    """True when a 409 body names Idempotency-Key reuse.  Never logs or returns the body."""
    try:
        data = json.loads(raw if isinstance(raw, str) else bytes(raw or b"").decode("utf-8", "replace"))
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    code = str(data.get("code") or "").strip().lower()
    name = str(data.get("name") or "").strip().lower()
    return code in KEY_REUSE_CODES or name in KEY_REUSE_NAMES


async def _read_error_body(resp: Any) -> bytes:
    """At most ERROR_BODY_MAX bytes of an error body; b"" if it can't be read."""
    try:
        content = getattr(resp, "content", None)
        if content is not None:
            chunks = bytearray()
            while len(chunks) < ERROR_BODY_MAX:
                chunk = await content.read(ERROR_BODY_MAX - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            return bytes(chunks)
        return b""
    except Exception:  # noqa: BLE001 - an unreadable body is just "ambiguous 409"
        return b""


def build_agentmail_body(payload: dict, recipient: str) -> dict | None:
    """The exact request body, or None if the payload does not match the pinned route."""
    to = payload.get("to")
    if not isinstance(to, list) or to != [recipient] or not recipient_allowed(recipient):
        return None
    if payload.get("category") != CATEGORY:
        return None
    subject, text, html = payload.get("subject"), payload.get("text"), payload.get("html")
    if not isinstance(subject, str) or not isinstance(text, str) or not isinstance(html, str):
        return None
    if not subject or len(subject) > SUBJECT_MAX or "\r" in subject or "\n" in subject:
        return None
    if len(text) > TEXT_MAX or len(html) > HTML_MAX:
        return None
    # Verbatim: what notify_render produced is what goes out.
    return {"to": [recipient], "subject": subject, "text": text, "html": html, "labels": list(LABELS)}


def build_gdit_agentmail_body(payload: dict, recipient: str) -> dict | None:
    """The redacted GDIT body, or None if the payload does not match the pinned route."""
    to = payload.get("to")
    if not isinstance(to, list) or to != [recipient] or not gdit_recipient_allowed(recipient):
        return None
    if payload.get("category") != CATEGORY_GDIT:
        return None
    subject, text, html = payload.get("subject"), payload.get("text"), payload.get("html")
    if not isinstance(subject, str) or not isinstance(text, str) or not isinstance(html, str):
        return None
    if not subject or len(subject) > SUBJECT_MAX or "\r" in subject or "\n" in subject:
        return None
    if len(text) > TEXT_MAX or len(html) > HTML_MAX:
        return None
    return {"to": [GDIT_RECIPIENT], "subject": subject, "text": text, "html": html, "labels": list(LABELS_GDIT)}


def agentmail_sender(get_session: Callable[[], Any], inbox: str, api_key: str,
                     recipient: str) -> Callable[[dict], Awaitable[tuple[bool, str, bool]]]:
    """Direct HTTPS POST to AgentMail.  No other host, no redirects."""

    async def send(job: dict) -> tuple[bool, str, bool]:
        if not sender_allowed(inbox) or not parse_recipient(recipient):
            return False, "blocked-address", False
        body = build_agentmail_body(dict(job.get("payload") or {}), recipient)
        if body is None:
            return False, "blocked-payload", False
        if not api_key:
            return False, "not-configured", True
        session = get_session()
        if session is None:
            return False, "no-session", True
        url = AGENTMAIL_API.format(inbox=quote(inbox.strip().lower(), safe="@."))
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        idem = idempotency_key(job)
        if idem:
            headers["Idempotency-Key"] = idem
        try:
            async with session.post(url, json=body, headers=headers, timeout=TIMEOUT,
                                    allow_redirects=False) as resp:
                if resp.status == 409 and conflict_is_key_reuse(await _read_error_body(resp)):
                    return False, "http-409-key-reused", False  # can never succeed: park now
                return _classify(resp.status)
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__.lower(), True

    return send


def agentmail_gdit_sender(get_session: Callable[[], Any], inbox: str, api_key: str,
                          recipient: str = GDIT_RECIPIENT) -> Callable[[dict], Awaitable[tuple[bool, str, bool]]]:
    """Direct HTTPS POST to AgentMail for the pinned GDIT address.  No redirects."""

    async def send(job: dict) -> tuple[bool, str, bool]:
        if not sender_allowed(inbox) or parse_gdit_recipient(recipient) != GDIT_RECIPIENT:
            return False, "blocked-address", False
        body = build_gdit_agentmail_body(dict(job.get("payload") or {}), recipient)
        if body is None:
            return False, "blocked-payload", False
        if not api_key:
            return False, "not-configured", True
        session = get_session()
        if session is None:
            return False, "no-session", True
        url = AGENTMAIL_API.format(inbox=quote(inbox.strip().lower(), safe="@."))
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        idem = gdit_idempotency_key(job)
        if idem:
            headers["Idempotency-Key"] = idem
        try:
            async with session.post(url, json=body, headers=headers, timeout=TIMEOUT,
                                    allow_redirects=False) as resp:
                if resp.status == 409 and conflict_is_key_reuse(await _read_error_body(resp)):
                    return False, "http-409-key-reused", False
                return _classify(resp.status)
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__.lower(), True

    return send


def push_sender(call_service: Callable[[str, str, dict], Awaitable[tuple[int, Any]]], service: str) -> Callable[[dict], Awaitable[tuple[bool, str, bool]]]:
    """HA Companion push through notify.<mobile_app_...>."""

    async def send(job: dict) -> tuple[bool, str, bool]:
        if not NOTIFY_RE.fullmatch(service or ""):
            return False, "not-configured", True
        payload = dict(job.get("payload") or {})
        data = {
            "title": str(payload.get("title") or "CallCatch")[:80],
            "message": str(payload.get("message") or "")[:200],
            "data": payload.get("data") or {},
        }
        try:
            status, _body = await call_service("notify", service, data)
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__.lower(), True
        return _classify(int(status or 0))

    return send
