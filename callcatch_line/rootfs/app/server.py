#!/usr/bin/env python3
"""CallCatch Line: the only internet-facing piece of the CallCatch phone agent.

A small MCP server (Streamable HTTP, JSON responses) at POST /mcp for the
Grok Voice phone agent, plus an optional call-ended webhook at
POST /hooks/call-ended.  It holds no Home Assistant token and mounts no
folders.  Its one upstream is CallCatch on the Supervisor network, reached
with a dedicated agent bearer.

Order on every request: path/method allowlist -> peer allowlist -> size
check (header) -> auth (constant time) -> lockout (failures only) -> rate
limit -> read body (capped, looped) -> parse.  A valid token is never locked
out, so a noisy source cannot block the real agent.
Logs carry tool, result code, reference and timing.  Never headers, bodies,
tokens, or caller text.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime
from typing import Any

import ratelimit
import tools
from aiohttp import ClientSession, ClientTimeout, web

VERSION = "0.2.1"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
OPTIONS_PATH = os.environ.get("LINE_OPTIONS", "/data/options.json")
MCP_BODY_MAX = 16 * 1024
HOOK_BODY_MAX = 64 * 1024
TELNYX_PATH = "/telnyx/events"
TELNYX_TOLERANCE_S = 300  # Telnyx: reject telnyx-timestamp more than 5 minutes off
TELNYX_EVENTS = ("call.initiated", "call.answered", "call.hangup", "call.recording.saved", "call.recording.error")
_TELNYX_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TELNYX_CONN_RE = re.compile(r"^[0-9]{1,32}$")
TELNYX_MAX_CONNECTIONS = 10
_TELNYX_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+(Z|[+-]\d{2}:?\d{2})$")
SESSION_TTL_S = 2 * 3600
SESSIONS_MAX = 200
UPSTREAM_TIMEOUT = ClientTimeout(total=8)
_HEADER_NAME_RE = re.compile(r"^X-[A-Za-z0-9\-]{1,40}$")
_HEADER_VAL_RE = re.compile(r"^[\x21-\x7e]{1,200}$")
_UPSTREAM_RE = re.compile(r"^http://([a-z0-9][a-z0-9\-]{0,62}):(\d{2,5})/?$")
_NUM_RE = re.compile(r"^\+?[0-9]{7,15}$")
_CONV_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,100}$")
_START_RE = re.compile(r"^[A-Za-z0-9_.:+\-/ ]{1,64}$")  # ISO 8601, epoch, or another short form
_ISO_SECONDS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:?\d{2})?$")
START_MIN, START_MAX = 1577836800, 4102444800  # 2020-01-01 .. 2100-01-01 UTC
DEFAULT_ALLOWED_PEERS = "172.30.32.0/23,127.0.0.1/32,::1/128"  # Supervisor network + own loopback
CALLER_GAP_S = 180  # sessionless + caller-ID only: a new call after this much quiet
WRITE_TOOLS = ("update_call", "take_message", "request_callback")
APP = web.AppKey("line", dict)


def load_options() -> dict:
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int(opts: dict, name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(opts.get(name, default))))
    except (TypeError, ValueError):
        return default


def _secret(value: Any, min_len: int = 32) -> str:
    text = str(value or "")
    return text if len(text) >= min_len else ""


def _upstream(raw: Any) -> str:
    m = _UPSTREAM_RE.fullmatch(str(raw or "").strip())
    return f"http://{m.group(1)}:{m.group(2)}" if m else ""


def _networks(raw: Any) -> list:
    nets = []
    for part in str(raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return nets


def _in_nets(addr: str, nets: list) -> bool:
    try:
        ip = ipaddress.ip_address((addr or "").strip())
    except ValueError:
        return False
    return any(ip.version == n.version and ip in n for n in nets)


def _header_name(raw: Any, default: str) -> str:
    text = str(raw or "").strip()
    return text if _HEADER_NAME_RE.fullmatch(text) else default


def _telnyx_public_key(raw: Any) -> tuple[bool, Any]:
    """(configured, key).  A non-empty value that does not parse stays configured with no key (fail closed)."""
    text = "".join(str(raw or "").split())
    if not text:
        return False, None
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = base64.b64decode(text, validate=True)
        if len(key) != 32:
            return True, None
        return True, Ed25519PublicKey.from_public_bytes(key)
    except Exception:  # noqa: BLE001 - bad key or library missing: reject every webhook
        return True, None


def _telnyx_connections(raw: Any) -> frozenset:
    """Allowlisted Telnyx connection ids.  Blank, too many, or any bad token gives an empty set (fail closed)."""
    tokens = [t for t in re.split(r"[\s,]+", str(raw or "").strip()) if t]
    if not tokens or len(tokens) > TELNYX_MAX_CONNECTIONS:
        return frozenset()
    if any(not _TELNYX_CONN_RE.fullmatch(t) for t in tokens):
        return frozenset()
    return frozenset(tokens)


def _telnyx_connection_of(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    text = str(value if value is not None else "").strip()
    return text if _TELNYX_CONN_RE.fullmatch(text) else ""


class Config:
    def __init__(self, opts: dict):
        self.mcp_token = _secret(opts.get("mcp_token"))
        self.agent_token = _secret(opts.get("callcatch_agent_token"))
        self.upstream = _upstream(opts.get("callcatch_url", "http://a1d3bd4b-voicemail:8099"))
        self.call_header = _header_name(opts.get("call_id_header"), "X-CallCatch-Call")
        self.caller_header = _header_name(opts.get("caller_header"), "X-CallCatch-From")
        self.start_header = _header_name(opts.get("start_header"), "X-CallCatch-Start")
        self.delete_ends_call = bool(opts.get("delete_ends_call", False))
        self.hook_token = _secret(opts.get("hook_token"))
        # Who may connect at all (M4), and whose CF-Connecting-IP is believed (M2).
        self.allowed_peers = _networks(opts.get("allowed_peers", DEFAULT_ALLOWED_PEERS)) or _networks(DEFAULT_ALLOWED_PEERS)
        self.trusted_proxies = _networks(opts.get("trusted_proxies", ""))
        sig = str(opts.get("hook_signing_secret") or "")
        self.hook_key = b""
        if sig:
            raw = sig.removeprefix("whsec_")
            try:
                self.hook_key = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                self.hook_key = b""
            if len(self.hook_key) < 16:
                self.hook_key = b""
        self.telnyx_configured, self.telnyx_key = _telnyx_public_key(opts.get("telnyx_public_key"))
        # 0.2.1: events from any other connection are never relayed.  Empty keeps the path off.
        self.telnyx_connections = _telnyx_connections(opts.get("telnyx_connection_ids"))
        self.rate_per_min = _int(opts, "rate_per_min", 60, 5, 600)
        self.tool_calls_per_min = _int(opts, "tool_calls_per_min", 30, 5, 300)
        self.finals_per_call = _int(opts, "finals_per_call", 6, 1, 50)
        self.finals_per_number_hour = _int(opts, "finals_per_number_hour", 6, 1, 100)
        self.finals_per_day = _int(opts, "finals_per_day", 40, 1, 1000)
        self.finals_no_caller_id_hour = _int(opts, "finals_no_caller_id_hour", 20, 1, 200)
        self.turns_per_call_min = _int(opts, "turns_per_call_min", 20, 1, 120)
        self.send_conversation_id = bool(opts.get("send_conversation_id", True))
        self.secrets = [s for s in (self.mcp_token, self.agent_token, self.hook_token) if s]
        if self.mcp_token and self.mcp_token in (self.agent_token, self.hook_token):
            self.mcp_token = ""  # refuse shared secrets: each credential has one job

    @property
    def ready(self) -> bool:
        return bool(self.mcp_token and self.agent_token and self.upstream)


# Session ids Line issues: secrets.token_urlsafe(24) is 32 URL-safe characters.
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_LOG_METHODS = {"tools/call", "tools/list", "ping", "notifications/initialized"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s callcatch_line: %(message)s", stream=sys.stdout)
log = logging.getLogger("callcatch_line")


def token_ok(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(hashlib.sha256(presented.encode()).digest(), hashlib.sha256(expected.encode()).digest())


def bearer(request: web.Request) -> str:
    auth = request.headers.get("Authorization") or ""
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


def call_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()[:32]


def last4(num: str) -> str:
    d = re.sub(r"\D", "", num or "")
    return ("•••" + d[-4:]) if len(d) >= 4 else "none"


def _state(app: web.Application) -> dict:
    return app[APP]


def _json(status: int, body: dict, headers: dict | None = None) -> web.Response:
    h = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if headers:
        h.update(headers)
    return web.json_response(body, status=status, headers=h)


def _rpc_error(rid: Any, code: int, message: str, status: int = 200) -> web.Response:
    return _json(status, {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


async def _read_capped(request: web.Request, limit: int) -> bytes | None:
    """Read the whole body (chunked or not) up to ``limit``.  None if larger."""
    if request.content_length is not None and request.content_length > limit:
        return None
    buf = bytearray()
    while True:
        chunk = await request.content.read(limit + 1 - len(buf))
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            return None


async def upstream(app: web.Application, method: str, path: str, body: dict | None = None,
                   params: dict | None = None) -> tuple[int, dict]:
    st = _state(app)
    cfg: Config = st["cfg"]
    session: ClientSession | None = st.get("session")
    if session is None:
        return 0, {}
    headers = {"Authorization": f"Bearer {cfg.agent_token}"}
    try:
        async with session.request(method, cfg.upstream + path, json=body, params=params,
                                   headers=headers, timeout=UPSTREAM_TIMEOUT) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                data = {}
            return resp.status, data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("upstream %s %s", path, type(exc).__name__)
        return 0, {}


# --- sessions ---------------------------------------------------------------

def _prune_sessions(st: dict, now: float) -> None:
    sessions = st["sessions"]
    for sid in [s for s, v in sessions.items() if now - v["last"] > SESSION_TTL_S]:
        del sessions[sid]
    while len(sessions) > SESSIONS_MAX:
        oldest = min(sessions, key=lambda s: sessions[s]["last"])
        del sessions[oldest]


def _caller_hint(request: web.Request, cfg: Config) -> str:
    raw = (request.headers.get(cfg.caller_header) or "").strip()
    raw = re.sub(r"[\s()\-.]", "", raw)
    return raw if _NUM_RE.fullmatch(raw) else ""


def _call_start(request: web.Request, cfg: Config) -> str:
    """The platform's call-start variable (start_header).  Format unknown, so
    any short printable value is kept as given; "" when unset or an unfilled
    template such as {{call_start_time}}."""
    raw = (request.headers.get(cfg.start_header) or "").strip()
    return raw if _START_RE.fullmatch(raw) else ""


def start_is_precise(start: str) -> bool:
    """True only for a seconds-resolution timestamp: epoch seconds or
    milliseconds in range, or ISO 8601 with hours, minutes and seconds.  A
    coarse value (a date, minutes only, free text) could repeat across two
    calls from one number, so it never becomes part of a call key (L1)."""
    t = (start or "").strip()
    if re.fullmatch(r"\d{10}(\.\d{1,6})?", t):
        return START_MIN <= float(t) <= START_MAX
    if re.fullmatch(r"\d{13}", t):
        return START_MIN <= int(t) / 1000 <= START_MAX
    if not _ISO_SECONDS_RE.fullmatch(t):
        return False
    try:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return START_MIN <= dt.timestamp() <= START_MAX


def _has_call_header(request: web.Request, cfg: Config) -> bool:
    per_call = (request.headers.get(cfg.call_header) or "").strip()
    if per_call and _HEADER_VAL_RE.fullmatch(per_call) and "{" not in per_call:
        return True
    return bool(_caller_hint(request, cfg) and start_is_precise(_call_start(request, cfg)))


def _conversation_id(request: web.Request, cfg: Config) -> str:
    """The platform's per-call conversation id (call_id_header), passed to
    CallCatch so the notice can name it.  "" when unset, unfilled or odd."""
    if not cfg.send_conversation_id:
        return ""
    raw = (request.headers.get(cfg.call_header) or "").strip()
    return raw if _CONV_RE.fullmatch(raw) else ""


def _call_extra(request: web.Request, cfg: Config) -> dict:
    out = {}
    conv, start = _conversation_id(request, cfg), _call_start(request, cfg)
    if conv:
        out["conversation_id"] = conv
    if start:
        out["call_start"] = start
    return out


def _key_for(request: web.Request, cfg: Config, sid: str) -> str:
    """Call key: the per-call id header, else caller number + call start
    (both platform variables, the same on every request of one call), else
    the MCP session.  "" if none."""
    per_call = (request.headers.get(cfg.call_header) or "").strip()
    if per_call and _HEADER_VAL_RE.fullmatch(per_call) and "{" not in per_call:
        return call_key("call", per_call)
    hint, start = _caller_hint(request, cfg), _call_start(request, cfg)
    if hint and start_is_precise(start):
        return call_key("start", f"{hint}|{start}")
    if sid:
        return call_key("sess", sid)
    return ""


def _caller_key(st: dict, hint: str, now: float) -> str:
    """Sessionless and no per-call header: one row per caller ID per call.

    A new segment starts after CALLER_GAP_S of quiet from that number, so
    one call never fans out into many rows (M3)."""
    seg = st["caller_segments"]
    n, last = seg.get(hint, (0, 0.0))
    if last and now - last > CALLER_GAP_S:
        n += 1
    seg[hint] = (n, now)
    while len(seg) > 2000:
        seg.pop(next(iter(seg)))
    return call_key("caller", f"{hint}:{n}")


# --- tools -------------------------------------------------------------------

def _text(result: str, data: dict | None = None, error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": result}], "isError": error}
    if data is not None:
        out["structuredContent"] = data
    return out


async def run_tool(app: web.Application, name: str, args: dict, key: str, hint: str,
                   conv: str = "", start: str = "") -> tuple[dict, str, str]:
    """Returns (MCP result, log code, ref)."""
    st = _state(app)
    extra: dict = {"conversation_id": conv} if conv else {}
    if start:
        extra["call_start"] = start
    if args.get("summary"):
        extra["summary"] = args["summary"]
    if name == "get_free_busy":
        params = {k: str(v) for k, v in args.items() if k in ("start_date", "days")}
        status, data = await upstream(app, "GET", "/api/agent/freebusy", params=params)
        if status == 200 and data.get("ok"):
            view = {k: data.get(k) for k in ("tz", "from", "days", "window", "busy", "note")}
            return _text(json.dumps(view), view), "ok", ""
        if status == 400:
            return _text("That date range is not available.  Use today or later, up to 14 days.", error=True), "bad-range", ""
        return _text("Busy times are not available right now.  Offer to take a message.", error=True), f"upstream-{status}", ""

    fields = {k: v for k, v in args.items()
              if k not in ("turn", "summary") and (k != "preferred_window" or name == "request_callback")}
    if name == "update_call":
        if "turn" in args:
            if not st["turns_call"].allow(key, time.monotonic()):
                return _text("Too many turns saved for this call this minute.  Keep talking; save the next one later.",
                             error=True), "limit-turns", ""
            extra["turn"] = args["turn"]
        status, data = await upstream(app, "POST", "/api/agent/call",
                                      {"op": "touch", "call_key": key, "fields": fields, "caller_hint": hint, **extra})
        ref = str(data.get("ref") or "")
        if status == 200:
            return _text(f"Saved so far.  Reference {ref}.", {"ref": ref}), "ok", ref
        return _text("Could not save right now.  Keep going and try again.", error=True), f"upstream-{status}", ""

    # take_message / request_callback: limits on finals
    now = time.monotonic()
    # Key on caller ID (L2); the spoken callback number is never the key.
    # No caller ID (N1): key on this call, plus a higher shared ceiling, so one
    # withheld caller cannot block the next.
    num = re.sub(r"\D", "", hint)
    if not st["finals_call"].allow(key, now):
        return _text("This message is already saved.", error=True), "limit-call", ""
    if num:
        if not st["finals_number"].allow(num, now):
            return _text("Messages from this number are paused for now.  Please try again later.", error=True), "limit-number", ""
    elif not st["finals_no_caller_id"].allow("_", now):
        return _text("The message line is busy right now.  Please try again later.", error=True), "limit-no-caller-id", ""
    if not st["finals_day"].allow("_", now):
        return _text("The message line is busy right now.  Please try again later.", error=True), "limit-day", ""
    kind = "callback" if name == "request_callback" else "message"
    status, data = await upstream(app, "POST", "/api/agent/call",
                                  {"op": "final", "call_key": key, "kind": kind, "fields": fields, "caller_hint": hint,
                                   **extra})
    ref = str(data.get("ref") or "")
    if status == 200:
        if kind == "callback":
            msg = f"Saved.  Reference {ref}.  Bryson will get the callback request.  No time is promised."
        else:
            msg = f"Saved.  Reference {ref}.  Bryson will get the message."
        return _text(msg, {"ref": ref}), "ok", ref
    return _text("Could not save the message right now.  Please ask the caller to try again shortly.", error=True), f"upstream-{status}", ""


# --- HTTP handlers -------------------------------------------------------------

def client_id(request: web.Request) -> str:
    """Lockout key.  CF-Connecting-IP is believed only from a configured
    tunnel peer (trusted_proxies); otherwise the TCP peer is the key."""
    cfg: Config = _state(request.app)["cfg"]
    peer = (request.remote or "").strip()
    raw = peer
    if cfg.trusted_proxies and _in_nets(peer, cfg.trusted_proxies):
        raw = (request.headers.get("CF-Connecting-IP") or peer).strip()
    return re.sub(r"[^0-9A-Fa-f:.]", "", raw)[:45] or "_"


def peer_allowed(request: web.Request) -> bool:
    cfg: Config = _state(request.app)["cfg"]
    return _in_nets(request.remote or "", cfg.allowed_peers)


def _auth_failed(request: web.Request, who: str) -> web.Response:
    """Lockout only throttles failures.  It never blocks a valid token."""
    st = _state(request.app)
    if st["lockout"].locked(who):
        return _json(429, {"error": "slow-down"}, {"Retry-After": "300"})
    st["lockout"].fail(who)
    log.warning("auth failed path=%s", request.path)
    return _json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})


def _gate(request: web.Request, expected: str) -> web.Response | None:
    st = _state(request.app)
    cfg: Config = st["cfg"]
    if not peer_allowed(request):
        return _json(403, {"error": "forbidden"})
    if request.headers.get("Origin"):
        return _json(403, {"error": "forbidden"})
    if not cfg.ready:
        return _json(503, {"error": "not-configured"})
    if not token_ok(bearer(request), expected):
        return _auth_failed(request, client_id(request))
    if not st["requests"].allow():
        return _json(429, {"error": "slow-down"}, {"Retry-After": "60"})
    return None


def _wants_sse(request: web.Request) -> bool:
    accept = request.headers.get("Accept") or ""
    return "text/event-stream" in accept and "application/json" not in accept


def _reply(request: web.Request, payload: dict, headers: dict | None = None) -> web.Response:
    if _wants_sse(request):
        h = {"Cache-Control": "no-store", "Content-Type": "text/event-stream"}
        if headers:
            h.update(headers)
        return web.Response(text=f"event: message\ndata: {json.dumps(payload)}\n\n", headers=h)
    return _json(200, payload, headers)


async def mcp_post(request: web.Request) -> web.Response:
    st = _state(request.app)
    cfg: Config = st["cfg"]
    if request.content_length is not None and request.content_length > MCP_BODY_MAX:
        return _json(413, {"error": "too-large"})
    denied = _gate(request, cfg.mcp_token)
    if denied:
        return denied
    raw = await _read_capped(request, MCP_BODY_MAX)
    if raw is None:
        return _json(413, {"error": "too-large"})
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _rpc_error(None, -32700, "Parse error", 400)
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method", ""), str):
        return _rpc_error(None, -32600, "Invalid Request", 400)
    method = msg.get("method")
    rid = msg.get("id")
    now = time.time()
    _prune_sessions(st, now)
    sid = (request.headers.get("Mcp-Session-Id") or "").strip()
    if sid and sid not in st["sessions"]:
        if method == "initialize":
            sid = ""
        elif _SID_RE.fullmatch(sid):
            # Unknown but well-formed: Line restarted (or the session aged out)
            # mid-call.  Re-create it for the same call instead of failing the
            # agent's tool call.  The call key is derived from the per-call
            # header or this session id, so it maps back to the same row.
            st["sessions"][sid] = {"key": _key_for(request, cfg, sid), "last": now}
            _prune_sessions(st, now)
            log.info("session re-created method=%s", method if method in _LOG_METHODS else "?")
        else:
            return _json(404, {"error": "unknown-session"})
    hint = _caller_hint(request, cfg)

    if method is None and ("result" in msg or "error" in msg):
        return web.Response(status=202)
    if rid is None and str(method).startswith("notifications/"):
        return web.Response(status=202)

    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        asked = str(params.get("protocolVersion") or "")
        version = asked if asked in PROTOCOLS else PROTOCOLS[0]
        sid = secrets.token_urlsafe(24)
        key = _key_for(request, cfg, sid)  # never "" here: the new session id is the fallback
        st["sessions"][sid] = {"key": key, "last": now}
        # Record the call the moment the agent connects, before any tool runs,
        # but only when the platform marked it as a real call (per-call id or
        # caller number).  A console "test connection" or tool discovery has
        # neither, so it does not create a phantom call.
        if _has_call_header(request, cfg) or hint:
            status, data = await upstream(request.app, "POST", "/api/agent/call",
                                          {"op": "touch", "call_key": key, "fields": {}, "caller_hint": hint,
                                           **_call_extra(request, cfg)})
            log.info("initialize ref=%s upstream=%s from=%s", data.get("ref") or "-", status, last4(hint))
        else:
            log.info("initialize without call id")
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "callcatch-line", "version": VERSION},
            "instructions": tools.INSTRUCTIONS,
        }
        return _reply(request, {"jsonrpc": "2.0", "id": rid, "result": result}, {"Mcp-Session-Id": sid})

    if sid:
        st["sessions"][sid]["last"] = now
    if method == "ping":
        return _reply(request, {"jsonrpc": "2.0", "id": rid, "result": {}})
    if method == "tools/list":
        return _reply(request, {"jsonrpc": "2.0", "id": rid, "result": {"tools": tools.TOOLS}})
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        name = str(params.get("name") or "")
        started = time.monotonic()
        if not st["tool_calls"].allow():
            return _reply(request, {"jsonrpc": "2.0", "id": rid,
                                    "result": _text("Too many requests.  Wait a moment.", error=True)})
        args, err = tools.validate(name, params.get("arguments"))
        if args is None:
            if err == "unknown-tool":
                return _rpc_error(rid, -32602, "Unknown tool")
            log.info("tool=%s code=%s", name if name in tools.TOOL_NAMES else "?", err)
            return _reply(request, {"jsonrpc": "2.0", "id": rid,
                                    "result": _text(f"Not saved: {err.replace('-', ' ')}.", error=True)})
        key = st["sessions"][sid]["key"] if sid else _key_for(request, cfg, "")
        if not key and name in WRITE_TOOLS:
            if hint:
                key = _caller_key(st, hint, time.monotonic())
            else:
                # No session, no per-call id, no caller ID: refuse rather than mint a row per request (M3).
                log.info("tool=%s code=no-call-id", name)
                return _reply(request, {"jsonrpc": "2.0", "id": rid, "result": _text(
                    "Not saved: this connection has no call id.  Reconnect and try again.", error=True)})
        result, code, ref = await run_tool(request.app, name, args, key, hint, _conversation_id(request, cfg),
                                           _call_start(request, cfg))
        log.info("tool=%s code=%s ref=%s ms=%d from=%s", name, code, ref or "-",
                 int((time.monotonic() - started) * 1000), last4(hint))
        return _reply(request, {"jsonrpc": "2.0", "id": rid, "result": result})
    if rid is None:
        return web.Response(status=202)
    return _rpc_error(rid, -32601, "Method not found")


async def mcp_get(_request: web.Request) -> web.Response:
    # No server-initiated stream.  Streamable HTTP allows 405 here.
    return web.Response(status=405, headers={"Allow": "POST, DELETE"})


async def mcp_delete(request: web.Request) -> web.Response:
    st = _state(request.app)
    cfg: Config = st["cfg"]
    denied = _gate(request, cfg.mcp_token)
    if denied:
        return denied
    sid = (request.headers.get("Mcp-Session-Id") or "").strip()
    sess = st["sessions"].pop(sid, None)
    if sess is None:
        if not _SID_RE.fullmatch(sid):
            return _json(404, {"error": "unknown-session"})
        # Lost in a restart: close it for the same call key instead of a 404.
        sess = {"key": _key_for(request, cfg, sid), "last": time.time()}
    if cfg.delete_ends_call:
        status, data = await upstream(request.app, "POST", "/api/agent/call", {"op": "ended", "call_key": sess["key"]})
        log.info("session closed ref=%s upstream=%s", data.get("ref") or "-", status)
    return web.Response(status=204)


def verify_standard_webhook(key: bytes, headers: Any, raw: bytes, now: float | None = None) -> bool:
    """Standard Webhooks v1 (what xAI uses for realtime.call.incoming)."""
    if not key:
        return False
    wid = headers.get("webhook-id") or ""
    ts = headers.get("webhook-timestamp") or ""
    sig = headers.get("webhook-signature") or ""
    if not wid or not ts.isdigit() or not sig:
        return False
    clock = time.time() if now is None else now
    if abs(clock - int(ts)) > 300:
        return False
    expected = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()
    for part in sig.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def _hook_fields(body: dict) -> tuple[str, str, Any, str]:
    """Accept a flat body or an event envelope {id, type, data:{...}}."""
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    call_id = str(data.get("call_id") or data.get("conversation_id") or body.get("call_id") or "")[:200]
    caller = str(data.get("from") or data.get("caller") or data.get("caller_number") or "")
    if not caller and isinstance(data.get("sip_headers"), list):
        for h in data["sip_headers"]:
            if isinstance(h, dict) and str(h.get("name") or "").lower() == "from":
                caller = str(h.get("value") or "")
    caller = re.sub(r"[\s()\-.]", "", caller)
    caller = caller if _NUM_RE.fullmatch(caller) else ""
    transcript = data.get("transcript") if "transcript" in data else data.get("turns")
    hook_id = str(body.get("id") or data.get("event_id") or "")[:80]
    return call_id, caller, transcript, hook_id


async def hook_post(request: web.Request) -> web.Response:
    st = _state(request.app)
    cfg: Config = st["cfg"]
    if not peer_allowed(request):
        return _json(403, {"error": "forbidden"})
    if not (cfg.hook_token or cfg.hook_key) or not cfg.ready:
        return _json(503, {"error": "hook-disabled"})
    if request.headers.get("Origin"):
        return _json(403, {"error": "forbidden"})
    if request.content_length is not None and request.content_length > HOOK_BODY_MAX:
        return _json(413, {"error": "too-large"})
    raw = await _read_capped(request, HOOK_BODY_MAX)
    if raw is None:
        return _json(413, {"error": "too-large"})
    authed = token_ok(bearer(request), cfg.hook_token) or verify_standard_webhook(cfg.hook_key, request.headers, raw)
    if not authed:
        return _auth_failed(request, client_id(request))
    if not st["hooks"].allow():
        return _json(429, {"error": "slow-down"}, {"Retry-After": "60"})
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _json(400, {"error": "bad-request"})
    if not isinstance(body, dict):
        return _json(400, {"error": "bad-request"})
    call_id, caller, transcript, hook_id = _hook_fields(body)
    payload = {"hook_id": hook_id or request.headers.get("webhook-id", "")[:80], "caller_hint": caller,
               "transcript": transcript}
    if call_id and _HEADER_VAL_RE.fullmatch(call_id):
        payload["call_key"] = call_key("call", call_id)
    elif caller:
        payload["fallback_key"] = call_key("hook", hook_id or secrets.token_hex(8))
    else:
        return _json(400, {"error": "no-call-id"})
    status, data = await upstream(request.app, "POST", "/api/agent/hook", payload)
    log.info("hook ref=%s upstream=%s dup=%s", data.get("ref") or "-", status, bool(data.get("duplicate")))
    if status == 200:
        return _json(200, {"ok": True})
    return _json(502 if status == 0 else 503, {"ok": False})


def verify_telnyx(key: Any, headers: Any, raw: bytes, now: float | None = None) -> bool:
    """Ed25519 over ``{telnyx-timestamp}|{raw body}``.  Fail closed: no key, bad sig, or stale timestamp."""
    if key is None:
        return False
    sig_b64 = (headers.get("telnyx-signature-ed25519") or "").strip()
    ts = (headers.get("telnyx-timestamp") or "").strip()
    if not sig_b64 or not ts.isdigit() or not 10 <= len(ts) <= 12:
        return False
    clock = time.time() if now is None else now
    if abs(clock - int(ts)) > TELNYX_TOLERANCE_S:
        return False
    try:
        sig = base64.b64decode(sig_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if len(sig) != 64:
        return False
    try:
        key.verify(sig, ts.encode() + b"|" + raw)
    except Exception:  # noqa: BLE001 - invalid signature
        return False
    return True


def _telnyx_clean_id(value: Any) -> str:
    text = str(value or "")
    return text if _TELNYX_ID_RE.fullmatch(text) else ""


def _telnyx_clean_ts(value: Any) -> str:
    text = str(value or "")
    return text if _TELNYX_ISO_RE.fullmatch(text) else ""


def _telnyx_event(body: dict) -> dict | None:
    """Strict v2 envelope.  None when the event is not one we relay."""
    data = body.get("data") if isinstance(body.get("data"), dict) else None
    if not isinstance(data, dict) or data.get("record_type") != "event":
        return None
    etype = data.get("event_type")
    if etype not in TELNYX_EVENTS:
        return None
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else None
    if not isinstance(payload, dict):
        return None
    out = {
        "event_id": _telnyx_clean_id(data.get("id")),
        "event_type": etype,
        "occurred_at": _telnyx_clean_ts(data.get("occurred_at")),
        "call_session_id": _telnyx_clean_id(payload.get("call_session_id")),
        "call_leg_id": _telnyx_clean_id(payload.get("call_leg_id")),
        "call_control_id": _telnyx_clean_id(payload.get("call_control_id")),
        "connection_id": _telnyx_connection_of(payload.get("connection_id")),
    }
    caller = re.sub(r"[\s()\-.]", "", str(payload.get("from") or ""))
    out["from"] = caller if _NUM_RE.fullmatch(caller) else ""
    if etype == "call.recording.saved":
        out["recording_id"] = _telnyx_clean_id(payload.get("recording_id"))
    if not out["event_id"] or not (
        out["call_session_id"] or out["call_leg_id"] or out["call_control_id"] or out.get("recording_id")
    ):
        return None
    if etype == "call.hangup":
        out["start_time"] = _telnyx_clean_ts(payload.get("start_time"))
        out["end_time"] = _telnyx_clean_ts(payload.get("end_time"))
    elif etype == "call.recording.saved":
        urls = payload.get("recording_urls") if isinstance(payload.get("recording_urls"), dict) else {}
        out["channels"] = "dual" if payload.get("channels") == "dual" else "single"
        out["recording_started_at"] = _telnyx_clean_ts(payload.get("recording_started_at"))
        out["recording_ended_at"] = _telnyx_clean_ts(payload.get("recording_ended_at"))
        out["recording_urls"] = {}
        for fmt in ("wav", "mp3"):
            url = urls.get(fmt)
            if isinstance(url, str) and url.startswith("https://") and len(url) <= 4096:
                out["recording_urls"][fmt] = url
        # Do not relay public_recording_urls.  Those stay reachable for as long as the file exists.
    return out


async def telnyx_post(request: web.Request) -> web.Response:
    """Signed Telnyx call events.  The raw body is verified, then a reduced event is relayed."""
    st = _state(request.app)
    cfg: Config = st["cfg"]
    if not peer_allowed(request):
        return _json(403, {"error": "forbidden"})
    if not cfg.telnyx_configured or not cfg.telnyx_connections or not cfg.agent_token or not cfg.upstream:
        return _json(503, {"error": "telnyx-disabled"})
    if request.headers.get("Origin"):
        return _json(403, {"error": "forbidden"})
    if request.content_length is not None and request.content_length > HOOK_BODY_MAX:
        return _json(413, {"error": "too-large"})
    raw = await _read_capped(request, HOOK_BODY_MAX)
    if raw is None:
        return _json(413, {"error": "too-large"})
    # A configured key that failed to parse, a bad signature, and a stale timestamp all fail closed.
    if not verify_telnyx(cfg.telnyx_key, request.headers, raw):
        return _auth_failed(request, client_id(request))
    if not st["telnyx"].allow():
        return _json(429, {"error": "slow-down"}, {"Retry-After": "60"})
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _json(400, {"error": "bad-request"})
    if not isinstance(body, dict):
        return _json(400, {"error": "bad-request"})
    event = _telnyx_event(body)
    if event is None:
        return _json(200, {"ok": True, "ignored": True})
    if event.get("connection_id") not in cfg.telnyx_connections:
        # Another connection on the account (or none): acknowledged, never relayed.
        log.info("telnyx %s ignored connection not allowed", event["event_type"])
        return _json(200, {"ok": True, "ignored": True})
    seen = st["telnyx_seen"]
    if event["event_id"] in seen:
        return _json(200, {"ok": True, "duplicate": True})
    status, data = await upstream(request.app, "POST", "/api/agent/telnyx", {"event": event})
    log.info("telnyx %s upstream=%s from=%s", event["event_type"], status, last4(event.get("from") or ""))
    if status == 200:
        seen[event["event_id"]] = time.time()
        while len(seen) > 2000:
            seen.pop(next(iter(seen)))
        return _json(200, {"ok": True, "duplicate": bool(data.get("duplicate"))})
    return _json(502 if status == 0 else 503, {"ok": False})


async def healthz(_request: web.Request) -> web.Response:
    return _json(200, {"ok": True})


async def not_found(_request: web.Request) -> web.Response:
    return _json(404, {"error": "not-found"})


async def on_startup(app: web.Application) -> None:
    _state(app)["session"] = ClientSession()
    cfg: Config = _state(app)["cfg"]
    log.info("CallCatch Line %s ready=%s hook=%s telnyx=%s delete_ends_call=%s", VERSION, cfg.ready,
             bool(cfg.hook_token or cfg.hook_key), cfg.telnyx_configured, cfg.delete_ends_call)


async def on_cleanup(app: web.Application) -> None:
    session = _state(app).get("session")
    if session is not None:
        await session.close()


def make_app(opts: dict | None = None) -> web.Application:
    cfg = Config(load_options() if opts is None else opts)
    app = web.Application(client_max_size=HOOK_BODY_MAX)
    app[APP] = {
        "cfg": cfg,
        "sessions": {},
        "lockout": ratelimit.Lockout(10, 60, 300),
        "caller_segments": {},
        "requests": ratelimit.Window(cfg.rate_per_min, 60),
        "tool_calls": ratelimit.Window(cfg.tool_calls_per_min, 60),
        "hooks": ratelimit.Window(30, 60),
        "telnyx": ratelimit.Window(120, 60),
        "telnyx_seen": {},
        "finals_call": ratelimit.Window(cfg.finals_per_call, 6 * 3600),
        "finals_number": ratelimit.Window(cfg.finals_per_number_hour, 3600),
        "finals_no_caller_id": ratelimit.Window(cfg.finals_no_caller_id_hour, 3600),
        "finals_day": ratelimit.Window(cfg.finals_per_day, 86400),
        "turns_call": ratelimit.Window(cfg.turns_per_call_min, 60),
    }
    app.router.add_post("/mcp", mcp_post)
    app.router.add_get("/mcp", mcp_get)
    app.router.add_delete("/mcp", mcp_delete)
    app.router.add_post("/hooks/call-ended", hook_post)
    app.router.add_post(TELNYX_PATH, telnyx_post)
    app.router.add_get("/healthz", healthz)
    app.router.add_route("*", "/{tail:.*}", not_found)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)  # access log would hold paths/IPs only, keep quiet
    port = int(os.environ.get("LINE_PORT", "8791"))
    web.run_app(make_app(), host="0.0.0.0", port=port, print=lambda *_: None)


if __name__ == "__main__":
    main()
