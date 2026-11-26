"""Opt-in send-only domain setup for the pinned CallCatch domain (0.8.9).

Uses the add-on's AgentMail key in place. The key is never logged or returned.
DNS is not changed. The only host contacted is api.agentmail.to.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote

from aiohttp import ClientTimeout

log = logging.getLogger("voicemail.sender_domain")

API = "https://api.agentmail.to"
DOMAIN = "callcatch.brysoncarter.com"
INBOX = "callcatch@callcatch.brysoncarter.com"
CREATE_DOMAIN = {"domain": DOMAIN, "inbound_enabled": False, "subdomains_enabled": False}
CREATE_INBOX = {"username": "callcatch", "domain": DOMAIN}
TIMEOUT = ClientTimeout(total=20)
BODY_MAX = 64 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
PAGE_RE = re.compile(r"^[A-Za-z0-9._~\-]{1,256}$")
CODE_RE = re.compile(r"^[a-z0-9-]{1,40}$")
DOMAIN_STATUSES = frozenset({"NOT_STARTED", "PENDING", "INVALID", "FAILED", "VERIFYING", "VERIFIED"})
RECORD_TYPES = frozenset({"TXT", "CNAME", "MX", "CAA"})
RECORD_STATUSES = frozenset({"MISSING", "INVALID", "VALID"})
STATES = frozenset({"off", "not-configured", "pending", "ready", "error"})
INBOX_STATES = frozenset({"skipped", "absent", "created", "present"})


def status_off() -> dict:
    return _shape(False, "off", "disabled")


def status_no_key() -> dict:
    return _shape(True, "not-configured", "no-key")


def status_pending() -> dict:
    return _shape(True, "pending", "starting")


def status_error(code: str) -> dict:
    return _shape(True, "error", _code(code))


def _shape(enabled: bool, state: str, code: str, **extra: Any) -> dict:
    out = {
        "enabled": enabled,
        "state": state,
        "domain_status": "",
        "records": [],
        "inbox": "skipped",
        "code": code,
    }
    out.update(extra)
    return out


def _code(code: str) -> str:
    text = str(code or "")
    return text if CODE_RE.fullmatch(text) else "error"


def _clip(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def public_records(domain: Any, secret: str) -> list[dict]:
    """type, name, value, priority, status. Drops every other field."""
    if not isinstance(domain, dict):
        return []
    rows = domain.get("records")
    if not isinstance(rows, list):
        return []
    out = []
    for rec in rows:
        if len(out) >= 20 or not isinstance(rec, dict):
            continue
        typ = str(rec.get("type") or "").upper()
        if typ not in RECORD_TYPES:
            continue
        name = _clip(rec.get("name"), 253)
        value = _clip(rec.get("value"), 1024)
        if secret:
            name = name.replace(secret, "***")
            value = value.replace(secret, "***")
        status = str(rec.get("status") or "").upper()
        if status not in RECORD_STATUSES:
            status = ""
        pri = rec.get("priority")
        priority = pri if type(pri) is int and 0 <= pri <= 65535 else None
        out.append({"type": typ, "name": name, "value": value, "priority": priority, "status": status})
    return out


def sanitize(raw: Any, secret: str) -> dict:
    """Rebuild a status dict so a key, an email, or an extra field cannot ride along."""
    data = raw if isinstance(raw, dict) else {}
    state = data.get("state")
    if state not in STATES:
        state = "error"
    domain_status = str(data.get("domain_status") or "")
    if domain_status not in DOMAIN_STATUSES:
        domain_status = ""
    inbox = data.get("inbox")
    if inbox not in INBOX_STATES:
        inbox = "skipped"
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        enabled = state != "off"
    out = {
        "enabled": enabled,
        "state": state,
        "domain_status": domain_status,
        "records": public_records({"records": data.get("records")}, str(secret or "")),
        "inbox": inbox,
        "code": _code(str(data.get("code") or "")),
    }
    hidden = str(secret or "")
    if hidden and hidden in json.dumps(out):
        out = json.loads(json.dumps(out).replace(hidden, "***"))
    return out


def _log_snap(snap: dict) -> None:
    log.info(
        "sender-domain state=%s domain_status=%s inbox=%s code=%s records=%s",
        snap.get("state"), snap.get("domain_status"), snap.get("inbox"), snap.get("code"),
        len(snap.get("records") or []),
    )
    for rec in snap.get("records") or []:
        # Value stays on /api/diag. The log keeps the public name and status only.
        log.info(
            "sender-domain record type=%s name=%s priority=%s status=%s",
            rec.get("type"), rec.get("name"), rec.get("priority"), rec.get("status"),
        )


def _domain_status(row: Any) -> str:
    if not isinstance(row, dict):
        return ""
    status = str(row.get("status") or "")
    return status if status in DOMAIN_STATUSES else ""


def _send_only(row: dict) -> bool:
    return row.get("inbound_enabled") is False and row.get("subdomains_enabled") is False


def _complete(row: dict) -> bool:
    return (
        isinstance(row.get("records"), list)
        and "status" in row
        and "inbound_enabled" in row
        and "subdomains_enabled" in row
    )


def _match_domain(rows: list) -> dict | None:
    for row in rows:
        if str(row.get("domain") or "").strip().lower() == DOMAIN:
            return row
    return None


def _has_inbox(rows: list) -> bool:
    for row in rows:
        if str(row.get("email") or "").strip().lower() == INBOX:
            return True
    return False


def _from_row(code: str, row: Any, *, state: str, inbox: str) -> dict:
    return {
        "enabled": True,
        "state": state,
        "domain_status": _domain_status(row),
        "records": row.get("records") if isinstance(row, dict) else [],
        "inbox": inbox,
        "code": code,
    }


async def _body(resp: Any) -> bytes:
    content = getattr(resp, "content", None)
    if content is None:
        return b""
    chunks = bytearray()
    while len(chunks) <= BODY_MAX:
        chunk = await content.read(BODY_MAX + 1 - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


async def _read_json(resp: Any) -> Any:
    raw = await _body(resp)
    if not raw or len(raw) > BODY_MAX:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


async def _request(session: Any, method: str, path: str, api_key: str, *,
                   params: dict | None = None, body: dict | None = None) -> tuple[int, Any]:
    if not path.startswith("/v0/") or ".." in path:
        return 0, None
    url = API + path
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    kwargs: dict[str, Any] = {"headers": headers, "timeout": TIMEOUT, "allow_redirects": False}
    if params is not None:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body
    async with session.request(method, url, **kwargs) as resp:
        status = int(getattr(resp, "status", 0) or 0)
        if 300 <= status < 400:
            await _body(resp)  # discard; never follow Location
            return status, None
        return status, await _read_json(resp)


async def _collect(session: Any, api_key: str, path: str, key: str) -> tuple[str, list]:
    token = ""
    rows: list = []
    for _ in range(5):
        params = {"limit": "50"}
        if token:
            params["page_token"] = token
        status, data = await _request(session, "GET", path, api_key, params=params)
        if status != 200 or not isinstance(data, dict):
            return f"http-{status}" if status else "http-0", []
        batch = data.get(key)
        if not isinstance(batch, list):
            return "bad-list", []
        rows.extend(item for item in batch if isinstance(item, dict))
        nxt = data.get("next_page_token")
        if not nxt:
            return "", rows
        if not isinstance(nxt, str) or not PAGE_RE.fullmatch(nxt):
            return "", rows
        token = nxt
    return "", rows


async def _load_domain(session: Any, api_key: str, row: dict) -> tuple[str, dict]:
    domain_id = str(row.get("domain_id") or "")
    if not ID_RE.fullmatch(domain_id):
        return "bad-id", row
    if _complete(row):
        return "", row
    status, data = await _request(session, "GET", "/v0/domains/" + quote(domain_id, safe=""), api_key)
    if status != 200 or not isinstance(data, dict):
        return f"http-{status}" if status else "http-0", row
    if not ID_RE.fullmatch(str(data.get("domain_id") or "")):
        return "bad-id", data
    return "", data


async def _ensure(session: Any, api_key: str) -> dict:
    code, rows = await _collect(session, api_key, "/v0/domains", "domains")
    if code:
        return status_error(code)
    row = _match_domain(rows)
    if row is None:
        status, data = await _request(session, "POST", "/v0/domains", api_key, body=CREATE_DOMAIN)
        if status == 409:
            code, rows = await _collect(session, api_key, "/v0/domains", "domains")
            if code:
                return status_error(code)
            row = _match_domain(rows)
            if row is None:
                return status_error("http-409")
        elif 200 <= status < 300 and isinstance(data, dict):
            row = data
        else:
            return status_error(f"http-{status}" if status else "http-0")
    code, row = await _load_domain(session, api_key, row)
    if code:
        return _from_row(code, row, state="error", inbox="skipped")
    if not _send_only(row):
        return _from_row("domain-flags", row, state="error", inbox="skipped")
    domain_id = str(row.get("domain_id") or "")
    vstatus, _data = await _request(
        session, "POST", "/v0/domains/" + quote(domain_id, safe="") + "/verify", api_key,
    )
    if not (200 <= vstatus < 300 or vstatus == 409):
        return _from_row(f"verify-http-{vstatus}", row, state="error", inbox="skipped")
    status, data = await _request(session, "GET", "/v0/domains/" + quote(domain_id, safe=""), api_key)
    if status == 200 and isinstance(data, dict):
        row = data
    elif status != 200:
        return _from_row(f"http-{status}" if status else "http-0", row, state="error", inbox="skipped")
    if _domain_status(row) != "VERIFIED":
        return _from_row("not-verified", row, state="pending", inbox="skipped")
    code, inboxes = await _collect(session, api_key, "/v0/inboxes", "inboxes")
    if code:
        return _from_row(code, row, state="error", inbox="absent")
    if _has_inbox(inboxes):
        return _from_row("ok", row, state="ready", inbox="present")
    cstatus, cdata = await _request(session, "POST", "/v0/inboxes", api_key, body=CREATE_INBOX)
    if (
        200 <= cstatus < 300
        and isinstance(cdata, dict)
        and str(cdata.get("email") or "").strip().lower() == INBOX
    ):
        return _from_row("ok", row, state="ready", inbox="created")
    if cstatus == 409:
        return _from_row("ok", row, state="ready", inbox="present")
    return _from_row(
        f"inbox-http-{cstatus}" if cstatus else "inbox-http-0", row, state="error", inbox="absent",
    )


async def ensure_sender_domain(session: Any, api_key: str) -> dict:
    """Register the send-only domain, verify, then create the one inbox. Safe to re-run."""
    secret = str(api_key or "")
    if not secret:
        snap = sanitize(status_no_key(), "")
        _log_snap(snap)
        return snap
    try:
        raw = await _ensure(session, secret)
    except Exception as exc:  # noqa: BLE001 - status code only; the message can echo the key
        raw = status_error(type(exc).__name__.lower())
    snap = sanitize(raw, secret)
    _log_snap(snap)
    return snap
