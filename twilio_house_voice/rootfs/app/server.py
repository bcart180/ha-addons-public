#!/usr/bin/env python3
"""CallCatch Voice: Media Streams bridge to xAI Realtime.

One asyncio process (aiohttp), same shape as the Trading Desk add-on:

  * POST /twilio/voice    TwiML that connects a bidirectional Media Stream
  * GET  /twilio/stream   Twilio Media Streams WebSocket (one session per CallSid).
                         The URL has no query string. The one-time token is a
                         <Parameter> and is checked on the start event.
  * POST /twilio/status   hangup / Connect action (idempotent by CallSid)
  * GET  /api/health      Supervisor watchdog, body is only {"ok": true}
  * GET  /api/trust       allow / block lists (ingress user or trust bearer)
  * POST /api/trust/add, /api/trust/remove
  * GET  /               ingress status page

Caller audio is μ-law 8 kHz. When the bridge is live it is forwarded as
base64 audio/pcmu to wss://api.x.ai/v1/realtime with no resample, which is the
path in the xAI phone cookbook. Assistant audio comes back the same way and is
passed straight to Twilio. On hangup the call is written as PCM16 WAV under
/data/voicemail/. With mix_recording (the default) that file is a mono mix of
the caller and the assistant, time-aligned; otherwise it is the caller track.
When the mounts exist the WAV is copied to /share and /media.

Python, not the cookbook's Node example: this repository ships add-ons as one
aiohttp process with /data/options.json and a secret redaction filter.
"""
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import secrets
import shutil
import sys
import time
import types
import urllib.parse
import xml.sax.saxutils as xml_escape

import aiohttp
from aiohttp import web

import audio
import share_import
import trust

VERSION = "0.3.2"
PORT = int(os.environ.get("THV_PORT", "8787"))
XAI_DEFAULT_URL = "wss://api.x.ai/v1/realtime"
INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}
TERMINAL_STATUS = {"completed", "busy", "failed", "no-answer", "canceled", "cancelled"}
CALL_ID_RE = re.compile(r"^[A-Za-z0-9]{10,64}$")
# Hard cap on webhook delivery attempts per CallSid (was 30). Each attempt is
# one post_webhook() call (itself up to 3 quick tries). After the cap, or
# after OUTBOX_MAX_AGE_S, the sidecar is marked ack_timeout and never re-posted.
WEBHOOK_ATTEMPT_CAP = 10
WEBHOOK_RETRY_S = 30
# Durable outbox: a finished call keeps retrying its HA webhooks with backoff
# 30 s, 1, 2, 4, 8, 15, 30 min, 1, 2, 4 h (10 attempts span about 4 h of
# backoff; probes that find HA not ready do not count), within
# OUTBOX_MAX_AGE_S, across restarts.
OUTBOX_BACKOFF_S = (30, 60, 120, 240, 480, 900, 1800, 3600, 7200, 14400)
OUTBOX_BACKOFF_MAX_S = OUTBOX_BACKOFF_S[-1]
OUTBOX_MAX_AGE_S = 24 * 3600
# HA readiness probe (no Core API): a GET to the HA webhook URL. HA answers 405
# when the webhook is registered (the CallCatch automations allow POST only)
# and 200 for an unregistered id, which is what a restarting Core does before
# automations load. While not ready a post is held (not an attempt). Once the
# hook is seen registered after being unready, wait HA_SETTLE_S (measured from
# the first registered probe, not re-armed) so pyscript can load. After
# HA_UNREADY_MAX_S of not-ready, post anyway and flag posted_unready.
HA_PROBE_TIMEOUT_S = 5
HA_SETTLE_S = 25
HA_UNREADY_MAX_S = 600
# While held, re-probe on a backoff (15 s, 30 s, 60 s, then every 120 s) rather
# than every 30 s, so a restart costs a handful of probes, not ~20 (L3).
HA_HOLD_BACKOFF_S = (15, 30, 60, 120)
# Skip the pre-post probe when this URL was confirmed ready (registered probe
# or a 2xx post) less than HA_READY_TTL_S ago and nothing failed since (L3).
HA_READY_TTL_S = 120
# Early hangup: after the caller hangs up, give xAI this long to deliver the
# transcription of the caller's last words before the bridge is torn down.
FINALIZE_DRAIN_S = 3.0
# Watchdog: finalize a session whose Twilio socket is gone or silent, whose
# terminal status arrived while the socket still looked open, or that ran
# past the hard cap. Also recovers calls journaled as open by a process that
# died mid-call.
WATCHDOG_TICK_S = 5
WATCHDOG_SOCKET_GONE_S = 5
WATCHDOG_SILENT_S = 45
WATCHDOG_STATUS_GRACE_S = 8
WATCHDOG_MAX_CALL_S = 3600
WATCHDOG_ORPHAN_S = 60
# Retention: final, fully acknowledged (or skipped) sidecars older than this
# are deleted by the watchdog, at most once per SIDECAR_PRUNE_EVERY_S. Audio
# files for the call are removed with the sidecar. index.json is kept.
SIDECAR_RETENTION_S = 30 * 24 * 3600
SIDECAR_PRUNE_EVERY_S = 3600
HANGUP_NOTES = {
    "before_stream": "[Caller hung up before the assistant answered]",
    "greeting": "[Caller hung up early, during the greeting]",
    "mid_reply": "[Caller hung up early, while the assistant was speaking]",
    "caller_speaking": "[Caller hung up early, mid-sentence]",
    "after_reply": "[Caller hung up]",
    "recovered": "[Call ended unexpectedly; transcript recovered after a restart]",
}
AUDIO_B64_LIMIT = 1_500_000
TOKEN_TTL_S = 120
# Twilio sends connected, then start, as soon as the socket upgrades.
# Anything longer just holds an unauthenticated socket open.
STREAM_START_WAIT_S = 5
# 1008 Policy Violation: the upgrade succeeded, the stream did not.
STREAM_REJECT_CODE = 1008

DEFAULT_INSTRUCTIONS = (
    "You are the house phone. Greet the caller briefly, ask who they are and "
    "why they are calling, and take a clear message if no one is available. "
    "Be warm and brief. Do not pretend to be a specific person. Do not read "
    "back codes, passwords, or account numbers."
)
FALLBACK_SAY = "Please leave a message and we will get back to you."
# Appended for allow-listed callers only. The caller is known to the house,
# so the agent skips the "who is this and why are you calling" screening.
ALLOW_NOTE = (
    "This caller is on the household allow list. Do not screen them or ask "
    "why they are calling; greet them warmly and take their message."
)
CALL_META_CAP = 200
SUPERVISOR_NET_PREFIX = "172.30."
# Published-port traffic forwarded by a host-network proxy appears from the
# Docker gateway; it never gets the bearer path.
DOCKER_GATEWAY = "172.30.32.1"

MIRROR_MOUNTS = (
    ("share", pathlib.Path("/share"), pathlib.Path("/share/twilio_house_voice/voicemail"), None),
    ("media", pathlib.Path("/media"), pathlib.Path("/media/voicemail"), None),
    ("local", pathlib.Path("/homeassistant/www"), pathlib.Path("/homeassistant/www/voicemail"), "/local/voicemail"),
)

log = logging.getLogger("twilio_house_voice")


# --------------------------------------------------------------------------
# secrets: never log these
# --------------------------------------------------------------------------

class Redact(logging.Filter):
    def __init__(self):
        super().__init__()
        self.secrets = []

    def filter(self, record):
        record.msg = redact(record.getMessage(), self.secrets)
        record.args = ()
        return True


REDACT = Redact()
_LOG_READY = False


def redact(text, secrets_list=None) -> str:
    text = str(text)
    items = list(secrets_list if secrets_list is not None else REDACT.secrets)
    env_tok = os.environ.get("SUPERVISOR_TOKEN", "")
    if env_tok:
        items.append(env_tok)
    for secret in items:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level_name, secrets_list):
    global _LOG_READY
    REDACT.secrets = [s for s in secrets_list if s and len(s) >= 4]
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))
    if not _LOG_READY:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler.addFilter(REDACT)
        root.addHandler(handler)
        logging.getLogger("aiohttp.access").disabled = True
        logging.getLogger("aiohttp.access").propagate = False
        logging.getLogger("aiohttp.client").setLevel(logging.WARNING)
        logging.getLogger("aiohttp.server").setLevel(logging.WARNING)
        _LOG_READY = True


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------

def load_options():
    path = os.environ.get("THV_OPTIONS", "/data/options.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log.warning("options file is not valid JSON; using defaults")
        return {}


def _as_bool(raw, key, default):
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class Opts:
    def __init__(self, raw=None):
        raw = raw or {}
        def s(key, default=""):
            value = raw.get(key, default)
            return default if value is None else str(value).strip()

        self.twilio_account_sid = s("twilio_account_sid")
        self.twilio_auth_token = s("twilio_auth_token")
        self.twilio_phone_number = s("twilio_phone_number")
        self.xai_api_key = s("xai_api_key")
        self.xai_voice = s("xai_voice", "eve") or "eve"
        self.xai_model = s("xai_model", "grok-voice-latest") or "grok-voice-latest"
        self.xai_instructions = s("xai_instructions").replace("\\n", "\n")
        self.instructions_file = s("instructions_file")
        self.public_base_url = s("public_base_url").rstrip("/")
        self.ha_webhook_transcript = s("ha_webhook_transcript")
        self.ha_webhook_audio = s("ha_webhook_audio")
        self.ha_webhook_token = s("ha_webhook_token")
        self.record_caller_audio = _as_bool(raw, "record_caller_audio", True)
        self.record_assistant_audio = _as_bool(raw, "record_assistant_audio", False)
        # Default on: CallCatch plays {CallSid}.wav and should hear both sides.
        self.mix_recording = _as_bool(raw, "mix_recording", True)
        self.greeting_mode = s("greeting_mode", "agent")
        if self.greeting_mode not in ("agent", "none"):
            self.greeting_mode = "agent"
        self.xai_bridge = s("xai_bridge", "live")
        if self.xai_bridge not in ("live", "stub"):
            self.xai_bridge = "live"
        self.include_audio_base64 = _as_bool(raw, "include_audio_base64", False)
        self.validate_twilio_signature = _as_bool(raw, "validate_twilio_signature", True)
        self.log_level = s("log_level", "info") or "info"
        self.xai_realtime_url = s("xai_realtime_url") or os.environ.get("THV_XAI_URL", "").strip()
        # Bearer for /api/trust from CallCatch (Supervisor network only).
        self.trust_api_token = s("trust_api_token")

    def secret_values(self):
        # Webhook URLs are capability links (they often embed the webhook id).
        return [
            self.twilio_account_sid,
            self.twilio_auth_token,
            self.xai_api_key,
            self.ha_webhook_token,
            self.ha_webhook_transcript,
            self.ha_webhook_audio,
            self.trust_api_token,
        ]

    def bridge_mode(self):
        if self.xai_bridge == "stub" or not self.xai_api_key:
            return "stub"
        return "live"


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def mask_phone(number) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < 4:
        return ""
    return "•••" + digits[-4:]


def safe_call_id(value) -> str:
    value = (value or "").strip()
    return value if CALL_ID_RE.fullmatch(value) else ""


# --------------------------------------------------------------------------
# Twilio signatures
# https://www.twilio.com/docs/usage/webhooks/webhooks-security
# --------------------------------------------------------------------------

def twilio_signature(auth_token: str, url: str, params: dict) -> str:
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def signature_ok(auth_token: str, url: str, params: dict, provided: str) -> bool:
    if not auth_token or not provided:
        return False
    expected = twilio_signature(auth_token, url, params)
    return hmac.compare_digest(expected, provided)


def signature_url(request, opts: Opts) -> str:
    path = request.path_qs
    if opts.public_base_url:
        return opts.public_base_url + path
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}{path}"


def twilio_unavailable(opts: Opts) -> str:
    """Why the public Twilio routes are closed, or "" when they are open.

    No auth token: the routes answer 503 for everyone (0.2.5+), including
    ingress and loopback. Token without public_base_url: also closed, and
    main() refuses to start (see startup_config_error).
    """
    if not opts.twilio_auth_token:
        return "twilio_auth_token is not set"
    if not opts.public_base_url:
        return "public_base_url is required when twilio_auth_token is set"
    return ""


def public_base_url_error(value: str) -> str:
    """Why public_base_url is not a bare https origin, or "".

    Twilio signs the exact public URL, so it must be ``https://host[:port]``
    with no path, query, fragment or credentials. The value itself is never
    echoed back (it could carry user:password).
    """
    if not value:
        return ""
    if not value.isascii():
        return "public_base_url must be plain ASCII (use the punycode xn-- form for an international name)"
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return "public_base_url is not a valid URL"
    del port
    if parts.scheme.lower() != "https":
        return "public_base_url must start with https://"
    if not parts.hostname:
        return "public_base_url must include a host name"
    if parts.hostname.endswith("."):
        return "public_base_url host must not end with a dot"
    if parts.netloc.endswith(":"):
        return "public_base_url must not have an empty port"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "public_base_url must not contain credentials"
    if parts.path not in ("", "/"):
        return "public_base_url must not contain a path"
    if parts.query or "?" in value:
        return "public_base_url must not contain a query string"
    if parts.fragment or "#" in value:
        return "public_base_url must not contain a fragment"
    if any(c.isspace() for c in value):
        return "public_base_url must not contain spaces"
    return ""


def startup_config_error(opts: Opts) -> str:
    """A configuration the add-on must refuse to run with, or ""."""
    url_problem = public_base_url_error(opts.public_base_url)
    if url_problem:
        return (
            url_problem
            + ": use the bare public origin Twilio calls, for example "
            "https://voice.example.com, and restart."
        )
    if opts.twilio_auth_token and not opts.public_base_url:
        return (
            "public_base_url is required when twilio_auth_token is set: Twilio signs the "
            "public https URL, so without it every real webhook would fail validation. "
            "Set public_base_url (for example https://voice.example.com) and restart."
        )
    return ""


def twilio_authorized(opts: Opts, remote, url, params, provided) -> bool:
    """Public Twilio routes: the X-Twilio-Signature must match the auth token.

    validate_twilio_signature cannot turn that off (0.2.3+). Loopback and the
    ingress proxy do not skip the check. With no token there is nothing to
    check against, so nothing is authorized (the routes answer 503 first).
    """
    if not opts.twilio_auth_token:
        return False
    return signature_ok(opts.twilio_auth_token, url, params, provided)


# --------------------------------------------------------------------------
# instructions file: /share and /data only
# --------------------------------------------------------------------------

def resolve_instructions_path(path: str) -> pathlib.Path:
    raw = (path or "").strip()
    p = pathlib.Path(raw)
    if not raw:
        return pathlib.Path("/share/twilio_house_voice/instructions.txt")
    if not p.is_absolute():
        p = pathlib.Path("/share/twilio_house_voice") / p
    return p.resolve(strict=False)


def path_under_allowed(path: pathlib.Path) -> bool:
    roots = [
        pathlib.Path("/share").resolve(strict=False),
        pathlib.Path("/data").resolve(strict=False),
    ]
    return any(path == root or root in path.parents for root in roots)


def load_instructions(opts: Opts) -> str:
    if opts.instructions_file:
        path = resolve_instructions_path(opts.instructions_file)
        if not path_under_allowed(path):
            log.warning("instructions_file rejected (must live under /share or /data)")
        elif not path.is_file():
            log.warning("instructions_file missing")
        else:
            try:
                text = path.read_text(encoding="utf-8")[:20_000].strip()
            except OSError:
                text = ""
            if text:
                return text
    if opts.xai_instructions.strip():
        return opts.xai_instructions.strip()[:20_000]
    return DEFAULT_INSTRUCTIONS


# --------------------------------------------------------------------------
# TwiML + xAI session
# --------------------------------------------------------------------------

def public_http_base(request, opts: Opts) -> str:
    if opts.public_base_url:
        return opts.public_base_url
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


def to_ws_base(http_base: str) -> str:
    if http_base.startswith("https://"):
        return "wss://" + http_base[len("https://"):]
    if http_base.startswith("http://"):
        return "ws://" + http_base[len("http://"):]
    return http_base


def xml_attr(value) -> str:
    return xml_escape.escape(str(value), {'"': "&quot;", "'": "&apos;"})


def build_twiml(http_base: str, token: str, call_sid: str, speak_fallback: bool) -> str:
    """Bidirectional <Connect><Stream>.

    The Stream url must not carry a query string. Twilio error 31920: <Stream>
    does not support query parameters, the handshake never returns 101, and
    the call dies in about a second. The one-time token is a <Parameter>.
    Twilio copies it onto the start event's customParameters.
    """
    stream = f"{to_ws_base(http_base)}/twilio/stream"
    action = f"{http_base}/twilio/status"
    say = f"<Say>{xml_escape.escape(FALLBACK_SAY)}</Say>" if speak_fallback else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"{say}"
        f'<Connect action="{xml_attr(action)}" method="POST">'
        f'<Stream url="{xml_attr(stream)}">'
        f'<Parameter name="token" value="{xml_attr(token)}"/>'
        f'<Parameter name="call_sid" value="{xml_attr(call_sid)}"/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )


def session_update_message(opts: Opts, instructions: str) -> dict:
    """μ-law passthrough. Rate is omitted: audio/pcmu is fixed at 8 kHz."""
    return {
        "type": "session.update",
        "session": {
            "voice": opts.xai_voice,
            "instructions": instructions,
            "turn_detection": {"type": "server_vad"},
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"language_hint": "en"},
                },
                "output": {"format": {"type": "audio/pcmu"}},
            },
        },
    }


def xai_ws_url(opts: Opts) -> str:
    if opts.xai_realtime_url:
        return opts.xai_realtime_url
    model = urllib.parse.quote(opts.xai_model, safe="")
    return f"{XAI_DEFAULT_URL}?model={model}"


def webhook_targets(opts: Opts):
    """(name, url, include_audio_b64) tuples. One URL gets the full body."""
    transcript = opts.ha_webhook_transcript
    audio_url = opts.ha_webhook_audio
    if not transcript and not audio_url:
        return []
    if transcript and audio_url and transcript == audio_url:
        return [("call", transcript, True)]
    rows = []
    if transcript:
        rows.append(("transcript", transcript, False))
    if audio_url:
        rows.append(("audio", audio_url, True))
    if len(rows) == 1:
        name, url, _flag = rows[0]
        return [(name, url, True)]
    return rows


def http_webhook(url: str) -> bool:
    parts = urllib.parse.urlparse(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def maybe_audio_b64(wav: bytes, enabled: bool):
    if not enabled:
        return None, None
    if not wav:
        return None, "empty"
    if len(wav) > AUDIO_B64_LIMIT:
        return None, "too_large"
    return base64.b64encode(wav).decode("ascii"), None


# --------------------------------------------------------------------------
# disk
# --------------------------------------------------------------------------

def voicemail_dir(app) -> pathlib.Path:
    path = pathlib.Path(app["data_dir"]) / "voicemail"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mirror_wav(src: pathlib.Path):
    """Copy a finished WAV onto share, media, and /config/www when mounted.

    homeassistant_config is not mapped by default (it would expose all of
    /config, including secrets). The /local copy runs only if that mount is
    already there.
    """
    found = {}
    for key, mount, dest_dir, url_prefix in MIRROR_MOUNTS:
        if not mount.is_dir() or not os.access(mount, os.W_OK):
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / src.name
            shutil.copy2(src, dest)
            found[key] = str(dest)
            if url_prefix:
                found["local_url_path"] = f"{url_prefix}/{src.name}"
        except OSError:
            if key not in app_mirror_warned:
                app_mirror_warned.add(key)
                log.warning("voicemail mirror failed mount=%s", key)
    return found


app_mirror_warned = set()


def write_bytes_atomic(path: pathlib.Path, data: bytes) -> bool:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return True
    except OSError:
        log.warning("voicemail write failed name=%s", path.name)
        return False


def write_json_atomic(path: pathlib.Path, payload: dict) -> bool:
    return write_bytes_atomic(path, json.dumps(payload, indent=2).encode("utf-8"))


def update_index(app, summary: dict):
    path = voicemail_dir(app) / "index.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        calls = current.get("calls") if isinstance(current, dict) else None
        if not isinstance(calls, list):
            calls = []
    except (OSError, json.JSONDecodeError):
        calls = []
    calls = [c for c in calls if c.get("call_id") != summary.get("call_id")]
    calls.insert(0, summary)
    write_json_atomic(path, {"calls": calls[:100]})


def load_index(app):
    path = voicemail_dir(app) / "index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    calls = data.get("calls") if isinstance(data, dict) else None
    return calls if isinstance(calls, list) else []


def sidecar_path(app, call_id: str) -> pathlib.Path:
    return voicemail_dir(app) / f"{call_id}.json"


def load_sidecar(app, call_id: str):
    path = sidecar_path(app, call_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------
# call session
# --------------------------------------------------------------------------

_TRANSCRIPT_PUNCT = ".,!?;:\"“”‘’()[]"


def _transcript_words(text: str) -> list:
    words = []
    for raw in (text or "").split():
        word = raw.strip(_TRANSCRIPT_PUNCT)
        if word:
            words.append(word.casefold())
    return words


def _word_span(haystack: list, needle: list) -> bool:
    """True when `needle` is a contiguous multi-word span of `haystack`."""
    span = len(needle)
    if span < 2 or span > len(haystack):
        return False
    for index in range(len(haystack) - span + 1):
        if haystack[index:index + span] == needle:
            return True
    return False


def speaker_label(role: str) -> str:
    """Rendered speaker name. The stored turn role is not changed.

    caller → Caller. house, assistant, and agent → Assistant. Any other
    non-caller role used to render as House and now renders as Assistant.
    """
    key = (role or "").strip().casefold()
    if key == "caller":
        return "Caller"
    return "Assistant"


def _caller_turn_revises(previous: str, new: str) -> bool:
    """Whether `new` is the same caller utterance as `previous`.

    xAI `input_audio_transcription.updated` carries the cumulative transcript.
    A later update can arrive after `completed` already marked the line final.
    Replace when the new text extends that line, or either side is a prefix
    or a multi-word superset of the other. A different utterance does not match.
    """
    prev_words = _transcript_words(previous)
    new_words = _transcript_words(new)
    if not prev_words or not new_words:
        return False
    prev = " ".join(prev_words)
    nxt = " ".join(new_words)
    if nxt.startswith(prev) or prev.startswith(nxt):
        return True
    return _word_span(new_words, prev_words) or _word_span(prev_words, new_words)


class CallSession:
    def __init__(self, app, call_id: str, peer: dict):
        self.app = app
        self.call_id = call_id
        self.peer_from = peer.get("from") or ""
        self.peer_to = peer.get("to") or ""
        self.direction = peer.get("direction") or ""
        self.stir_verstat = trust.clean_verstat(peer.get("stir_verstat"))
        self.trust_verdict = peer.get("trust_verdict") or "screen"
        self.stream_sid = None
        self.twilio_ws = None
        self.caller_tl = audio.MulawTimeline()
        self.assistant_tl = audio.MulawTimeline()
        # Local monotonic instant of the first Media Stream `start`. Assistant
        # playback is scheduled on this clock. Caller frames prefer Twilio's
        # own timestamp, which is a different clock (see DOCS.md).
        self.stream_mono = None
        self.caller_ts_offset = 0
        self.playback_end_ms = 0.0
        self.timestamp_fallback = False
        self.turns = []
        self.started = time.time()
        self.started_at = iso_now()
        self.finished = False
        self.lock = asyncio.Lock()
        self.audio_lock = asyncio.Lock()
        self.xws = None
        self.xai_task = None
        self.xai_ready = False
        self.xai_connected = False
        self.xai_error = None
        self.pre_xai = []
        self.drop_assistant = False
        self.greeted = False
        self.truncated = False
        self.bridge = app["opts"].bridge_mode()
        self.replaced = 0
        self.reason = ""
        # Early-hangup bookkeeping.
        self.responding = False
        self.responses_done = 0
        self.awaiting_caller = False
        self.last_frame = time.monotonic()
        self.status_terminal_at = None
        self.hangup = None
        self.finalized = False
        self._final_task = None

    def add_turn(self, role: str, text: str, final: bool, cumulative: bool = False):
        text = (text or "").strip()
        # Empty partials, including a cumulative update with no words, are
        # dropped so they neither wipe the open line nor add a blank one.
        if not text:
            return
        if self.turns and self.turns[-1]["role"] == role and not self.turns[-1].get("final"):
            if cumulative or final:
                self.turns[-1]["text"] = text
            else:
                self.turns[-1]["text"] += text
            self.turns[-1]["final"] = final
            return
        if (
            final
            and self.turns
            and self.turns[-1]["role"] == role
            and self.turns[-1].get("final")
            and self.turns[-1]["text"] == text
        ):
            return
        if role == "caller":
            prior = None
            for turn in reversed(self.turns):
                if turn["role"] == role:
                    prior = turn
                    break
            if prior is not None and _caller_turn_revises(prior["text"], text):
                # Keep final sticky. Clearing it would let the next unrelated
                # caller utterance overwrite this line through the open-turn path.
                prior["text"] = text
                if final:
                    prior["final"] = True
                return
        self.turns.append({"role": role, "text": text, "at": iso_now(), "final": final})

    async def on_twilio(self, event: dict):
        if self.finished:
            return
        self.last_frame = time.monotonic()
        kind = event.get("event")
        if kind == "start":
            start = event.get("start") or {}
            new_sid = start.get("streamSid") or self.stream_sid
            if self.stream_mono is None:
                self.stream_mono = self.clock()
            if self.stream_sid and new_sid and new_sid != self.stream_sid:
                # Replacement stream: inbound timestamps restart at 0, and
                # audio queued on the old socket will not play.
                self.barge_in()
                self.caller_ts_offset = self.caller_tl.end_sample()
            self.stream_sid = new_sid
            encoding = ((start.get("mediaFormat") or {}).get("encoding") or "")
            if encoding and encoding != "audio/x-mulaw":
                log.warning("unexpected media encoding call_id=%s encoding=%s", self.call_id, encoding)
            got = safe_call_id(start.get("callSid") or "")
            if got and got != self.call_id:
                log.warning("stream call sid mismatch call_id=%s", self.call_id)
            if self.bridge == "live" and self.xai_task is None:
                self.xai_task = asyncio.create_task(self.xai_loop())
            else:
                log.info("stub bridge call_id=%s", self.call_id)
        elif kind == "media":
            media = event.get("media") or {}
            track = media.get("track") or "inbound"
            if track not in ("inbound", "inbound_track"):
                return
            payload = media.get("payload") or ""
            try:
                raw = base64.b64decode(payload, validate=False)
            except (ValueError, TypeError):
                return
            opts = self.app["opts"]
            if opts.record_caller_audio and raw:
                self.record_caller(raw, _media_timestamp_ms(media))
            if self.bridge == "live" and payload:
                await self.forward_in(payload)
        elif kind == "dtmf":
            digit = str((event.get("dtmf") or {}).get("digit") or "")
            if digit and len(digit) <= 4:
                self.add_turn("caller", f"[dtmf {digit}]", final=True)
        elif kind == "stop":
            await self.finalize("stop")

    async def forward_in(self, payload: str):
        async with self.audio_lock:
            if not (self.xai_ready and self.xws is not None and not self.xws.closed):
                if len(self.pre_xai) < 50:
                    self.pre_xai.append(payload)
                return
            await self._send_append(payload)

    async def _send_append(self, payload: str):
        try:
            await self.xws.send_json({"type": "input_audio_buffer.append", "audio": payload})
        except Exception:
            self.xai_error = "append_failed"

    async def xai_loop(self):
        opts = self.app["opts"]
        url = xai_ws_url(opts)
        headers = {
            "Authorization": f"Bearer {opts.xai_api_key}",
            "User-Agent": f"twilio-house-voice/{VERSION}",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
            async with self.app["client"].ws_connect(url, headers=headers, heartbeat=20, timeout=timeout) as xws:
                self.xws = xws
                self.xai_connected = True
                log.info("xai connected call_id=%s", self.call_id)
                instructions = load_instructions(opts)
                if self.trust_verdict == "allow":
                    instructions = instructions + "\n\n" + ALLOW_NOTE
                await xws.send_json(session_update_message(opts, instructions))
                async for msg in xws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            event = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            await self.on_xai(event)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                        aiohttp.WSMsgType.CLOSE,
                    ):
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.xai_error = type(exc).__name__
            log.warning("xai bridge down call_id=%s err=%s", self.call_id, type(exc).__name__)
        finally:
            self.xws = None
            self.xai_ready = False

    async def on_xai(self, event: dict):
        kind = event.get("type") or ""
        if kind == "session.updated":
            if not self.finished:
                await self._mark_ready()
        elif kind == "input_audio_buffer.speech_started":
            self.awaiting_caller = True
            self.drop_assistant = True
            if not self.finished:
                await self.clear_twilio()
        elif kind == "input_audio_buffer.committed":
            self.awaiting_caller = True
        elif kind == "response.created":
            self.responding = True
            self.drop_assistant = False
        elif kind in ("response.done", "response.cancelled"):
            self.responding = False
            self.responses_done += 1
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            delta = event.get("delta") or event.get("audio") or ""
            if delta and not self.drop_assistant and not self.finished:
                await self.play_assistant(delta)
        elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            self.add_turn("assistant", event.get("delta") or "", final=False)
        elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            self.add_turn("assistant", event.get("transcript") or "", final=True)
            self.checkpoint()
        elif kind == "conversation.item.input_audio_transcription.updated":
            self.add_turn("caller", event.get("transcript") or "", final=False, cumulative=True)
        elif kind == "conversation.item.input_audio_transcription.completed":
            self.awaiting_caller = False
            self.add_turn("caller", event.get("transcript") or "", final=True)
            self.checkpoint()
        elif kind == "conversation.item.input_audio_transcription.failed":
            self.awaiting_caller = False
        elif kind == "error":
            err = event.get("error") or {}
            code = err.get("code") or err.get("type") or "error"
            self.xai_error = str(code)[:80]
            log.warning("xai error call_id=%s code=%s", self.call_id, self.xai_error)

    async def _mark_ready(self):
        opts = self.app["opts"]
        async with self.audio_lock:
            self.xai_ready = True
            queued = list(self.pre_xai)
            self.pre_xai.clear()
            xws = self.xws
            greet = opts.greeting_mode == "agent" and not self.greeted and xws is not None
            if greet:
                self.greeted = True
            if xws is not None and greet:
                try:
                    await xws.send_json({"type": "response.create"})
                except Exception:
                    self.xai_error = "greet_failed"
            for payload in queued:
                if self.xws is None:
                    break
                await self._send_append(payload)

    def clock(self):
        return time.monotonic()

    def stream_ms(self) -> float:
        if self.stream_mono is None:
            self.stream_mono = self.clock()
        return (self.clock() - self.stream_mono) * 1000.0

    def _capture_assistant(self) -> bool:
        opts = self.app["opts"]
        return bool(opts.record_assistant_audio or opts.mix_recording)

    def record_caller(self, raw: bytes, timestamp_ms):
        if not raw:
            return
        if timestamp_ms is None:
            start = self.caller_tl.end_sample()
        else:
            start = audio.ms_to_sample(timestamp_ms) + self.caller_ts_offset
            end = self.caller_tl.end_sample()
            if start > end + audio.MAX_GAP_SAMPLES:
                if not self.timestamp_fallback:
                    self.timestamp_fallback = True
                    log.warning("caller timestamp ignored call_id=%s", self.call_id)
                start = end
        self.caller_tl.place(start, raw)
        if self.caller_tl.truncated and not self.truncated:
            self.truncated = True
            log.warning("caller audio capped call_id=%s", self.call_id)

    def record_assistant(self, raw: bytes):
        """Schedule assistant μ-law the way Twilio plays it.

        A burst is queued behind audio that has not finished, at 8 kHz. It is
        not stacked on the single instant the delta arrived.
        """
        if not raw:
            return
        now = self.stream_ms()
        start_ms = now if now >= self.playback_end_ms else self.playback_end_ms
        start = audio.ms_to_sample(start_ms)
        written = self.assistant_tl.place(start, raw)
        if written:
            self.playback_end_ms = start_ms + (written * 1000.0 / audio.MULAW_RATE)
        if self.assistant_tl.truncated and not self.truncated:
            self.truncated = True
            log.warning("assistant audio capped call_id=%s", self.call_id)

    def barge_in(self):
        """Drop assistant audio that has not reached the play cursor yet."""
        now = self.stream_ms()
        self.assistant_tl.truncate(audio.ms_to_sample(now))
        self.playback_end_ms = now

    async def play_assistant(self, payload_b64: str):
        ws = self.twilio_ws
        if ws is None or ws.closed or not self.stream_sid:
            return
        try:
            await ws.send_json({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload_b64},
            })
        except Exception:
            log.warning("twilio playback failed call_id=%s", self.call_id)
            return
        if not self._capture_assistant():
            return
        try:
            raw = base64.b64decode(payload_b64, validate=False)
        except (ValueError, TypeError):
            return
        if raw:
            self.record_assistant(raw)

    async def clear_twilio(self):
        # Trim even if the socket is already gone: that audio will not play.
        self.barge_in()
        ws = self.twilio_ws
        if ws is None or ws.closed or not self.stream_sid:
            return
        try:
            await ws.send_json({"event": "clear", "streamSid": self.stream_sid})
        except Exception:
            return

    def transcript_text(self) -> str:
        lines = []
        for turn in self.turns:
            if turn.get("text"):
                lines.append(f"{speaker_label(turn.get('role'))}: {turn['text']}")
        note = (self.hangup or {}).get("note")
        if note and (self.hangup or {}).get("early"):
            lines.append(note)
        return "\n".join(lines)

    def caller_pending(self) -> bool:
        """True while xAI may still owe us the caller's last words."""
        if self.awaiting_caller:
            return True
        for turn in reversed(self.turns):
            if turn["role"] == "caller":
                return not turn.get("final")
        return False

    def classify_hangup(self, reason: str) -> dict:
        """Who ended the call and when. The assistant never hangs up itself,
        so every stream stop / disconnect / terminal status is the caller (or
        the network). "early" means the caller left before the conversation
        reached a natural pause."""
        caller_words = any(t["role"] == "caller" and t.get("text") for t in self.turns)
        if reason.startswith("recovered"):
            phase = "recovered"
        elif self.awaiting_caller or (self.turns and self.turns[-1]["role"] == "caller" and not self.turns[-1].get("final")):
            phase = "caller_speaking"
        elif self.responding and self.responses_done == 0:
            phase = "greeting"
        elif self.responding:
            phase = "mid_reply"
        elif not caller_words:
            phase = "greeting"
        else:
            phase = "after_reply"
        early = phase != "after_reply"
        by = "system" if reason in ("shutdown",) or reason.startswith(("watchdog:max", "recovered")) else "caller"
        return {"by": by, "phase": phase, "early": early, "reason": reason, "note": HANGUP_NOTES.get(phase, "")}

    async def drain_xai(self):
        """Let xAI finish transcribing the caller's last words.

        Before this, finalize cancelled the bridge at once, so a caller who
        spoke and hung up before xAI emitted transcription.completed lost that
        line. Commit the buffer and wait (bounded) for the transcript.
        """
        if self.bridge != "live" or not self.caller_pending():
            return False
        xws = self.xws
        if xws is None or xws.closed:
            return False
        try:
            await xws.send_json({"type": "input_audio_buffer.commit"})
        except Exception:
            return False
        deadline = time.monotonic() + FINALIZE_DRAIN_S
        while time.monotonic() < deadline and self.caller_pending():
            task = self.xai_task
            if task is None or task.done() or self.xws is None:
                break
            await asyncio.sleep(0.05)
        return not self.caller_pending()

    def journal(self) -> dict:
        return {
            "addon": "twilio_house_voice",
            "version": VERSION,
            "state": "open",
            "call_id": self.call_id,
            "event": "hangup",
            "from": self.peer_from,
            "to": self.peer_to,
            "direction": self.direction,
            "started_at": self.started_at,
            "xai_bridge": self.bridge,
            "stir_verstat": self.stir_verstat,
            "trust_verdict": self.trust_verdict,
            "responses_done": self.responses_done,
            "turns": [dict(t) for t in self.turns if t.get("text")],
        }

    def checkpoint(self):
        """Journal the live call so a crash or restart still yields a transcript."""
        if self.finalized:
            return
        try:
            write_json_atomic(sidecar_path(self.app, self.call_id), self.journal())
        except Exception:  # noqa: BLE001 - journaling must never break a call
            log.warning("journal write failed call_id=%s", self.call_id)

    async def finalize(self, reason: str):
        """Finish the call exactly once, whatever path gets here first.

        The work runs in its own task and callers await it through shield():
        when Twilio drops the socket, aiohttp may cancel the stream handler,
        and before 0.2.4 that cancellation could abort the webhook post
        mid-flight, leaving a sidecar with no delivery and no index row.
        """
        if self.finished and self._final_task is None:
            return None
        if self._final_task is None:
            self.finished = True
            self.reason = reason
            self._final_task = asyncio.ensure_future(self._finalize_impl(reason))
            self.app.setdefault("finalizing", set()).add(self._final_task)
            self._final_task.add_done_callback(self.app["finalizing"].discard)
            first = True
        else:
            first = False
        try:
            record = await asyncio.shield(self._final_task)
        except asyncio.CancelledError:
            raise
        return record if first else None

    async def _finalize_impl(self, reason: str):
        try:
            drained = await self.drain_xai()
            if drained:
                log.info("caller transcript drained call_id=%s", self.call_id)
        except Exception:  # noqa: BLE001
            pass
        self.hangup = self.classify_hangup(reason)
        task = self.xai_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for turn in self.turns:
            turn["final"] = True
        try:
            record = await persist_call(self)
        except Exception as exc:
            log.warning("persist failed call_id=%s err=%s", self.call_id, type(exc).__name__)
            record = await persist_fallback(self)
        finally:
            self.finalized = True
            self.app["sessions"].pop(self.call_id, None)
        stats = self.app["stats"]
        stats["calls"] = stats.get("calls", 0) + 1
        stats["last"] = {
            "call_id": self.call_id,
            "ended_at": record.get("ended_at"),
            "from": mask_phone(self.peer_from),
            "audio": bool(record.get("audio_path")),
            "bridge": self.bridge,
        }
        log.info(
            "hangup call_id=%s reason=%s phase=%s early=%s audio_bytes=%s bridge=%s",
            self.call_id,
            reason,
            self.hangup.get("phase"),
            self.hangup.get("early"),
            record.get("audio_bytes") or 0,
            self.bridge,
        )
        return record


def _wav_for(raw: bytes):
    if not raw:
        return b"", 0
    return audio.mulaw_wav(raw), len(raw)


def _media_timestamp_ms(media: dict):
    raw = media.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _main_mulaw(session: CallSession, opts: Opts):
    """Bytes for {CallSid}.wav, plus whether that file is a mix.

    Caller-only when mixing is off or the assistant timeline kept no samples
    (stub bridge, no speech, or a clear that dropped every assistant frame).
    """
    caller = session.caller_tl.render() if opts.record_caller_audio else b""
    capture = opts.record_assistant_audio or opts.mix_recording
    assistant = session.assistant_tl.render() if capture else b""
    voiced = session.assistant_tl.voiced if capture else 0
    if opts.mix_recording and opts.record_caller_audio and voiced > 0:
        return audio.mix_mulaw(caller, assistant), assistant, voiced, True
    if opts.record_caller_audio:
        return caller, assistant, voiced, False
    return b"", assistant, voiced, False


async def persist_call(session: CallSession) -> dict:
    app = session.app
    opts = app["opts"]
    ended = time.time()
    main_raw, assistant_raw, assistant_voiced, mixed = _main_mulaw(session, opts)
    main_wav, main_bytes = _wav_for(main_raw)
    assistant_wav = b""
    if opts.record_assistant_audio and assistant_voiced > 0:
        assistant_wav, _ = _wav_for(assistant_raw)
    directory = voicemail_dir(app)
    audio_path = None
    assistant_path = None
    mirrors = {}
    main_wav_bytes = main_wav
    if main_wav:
        audio_file = directory / f"{session.call_id}.wav"
        if write_bytes_atomic(audio_file, main_wav):
            audio_path = str(audio_file)
            mirrors = mirror_wav(audio_file)
        else:
            main_wav_bytes = b""
    if assistant_wav:
        assistant_file = directory / f"{session.call_id}.assistant.wav"
        if write_bytes_atomic(assistant_file, assistant_wav):
            assistant_path = str(assistant_file)
    wall = round(max(0.0, ended - session.started), 2)
    record = {
        "addon": "twilio_house_voice",
        "version": VERSION,
        "call_id": session.call_id,
        "event": "hangup",
        "reason": session.reason,
        "from": session.peer_from,
        "to": session.peer_to,
        "direction": session.direction,
        "started_at": session.started_at,
        "ended_at": iso_now(),
        "duration_s": wall,
        "audio_seconds": round(main_bytes / audio.MULAW_RATE, 2) if main_bytes else 0,
        "xai_bridge": session.bridge,
        "xai_connected": session.xai_connected,
        "xai_error": session.xai_error,
        "greeting_mode": opts.greeting_mode,
        "stir_verstat": session.stir_verstat,
        "stir": trust.parse_stir(session.stir_verstat),
        "trust_verdict": session.trust_verdict,
        "transcript": [
            {"role": t["role"], "text": t["text"], "at": t["at"]}
            for t in session.turns
            if t.get("text")
        ],
        "transcript_text": session.transcript_text(),
        "audio_path": audio_path,
        "audio_bytes": main_bytes,
        "audio_mix": mixed,
        "caller_audio_bytes": session.caller_tl.voiced if opts.record_caller_audio else 0,
        "assistant_audio_path": assistant_path,
        "assistant_audio_bytes": assistant_voiced,
        "audio_share_path": mirrors.get("share"),
        "audio_media_path": mirrors.get("media"),
        "audio_local_path": mirrors.get("local"),
        "local_url_path": mirrors.get("local_url_path"),
        "truncated": session.truncated,
        "hangup": {k: v for k, v in (session.hangup or {}).items() if k != "note"},
        "early_hangup": bool((session.hangup or {}).get("early")),
        "state": "final",
        "ha_post": {},
    }
    # audio_base64 is attached only on the webhook body, never written to disk.
    # This is the same WAV CallCatch plays ({CallSid}.wav), mix or caller-only.
    record["_caller_wav"] = main_wav_bytes
    write_json_atomic(sidecar_path(app, session.call_id), {k: v for k, v in record.items() if k != "_caller_wav"})
    await deliver_webhooks(app, record)
    _remember_index(app, record)
    return record


def _base_record(call_id: str, reason: str, journal: dict, hangup: dict) -> dict:
    turns = [t for t in (journal.get("turns") or []) if isinstance(t, dict) and t.get("text")]
    lines = [f"{speaker_label(t.get('role'))}: {t['text']}" for t in turns]
    if hangup.get("early") and hangup.get("note"):
        lines.append(hangup["note"])
    verstat = trust.clean_verstat(journal.get("stir_verstat"))
    return {
        "addon": "twilio_house_voice",
        "version": VERSION,
        "call_id": call_id,
        "event": "hangup",
        "reason": reason,
        "from": journal.get("from") or "",
        "to": journal.get("to") or "",
        "direction": journal.get("direction") or "",
        "started_at": journal.get("started_at") or iso_now(),
        "ended_at": iso_now(),
        "duration_s": 0,
        "audio_seconds": 0,
        "xai_bridge": journal.get("xai_bridge") or "",
        "xai_connected": False,
        "stir_verstat": verstat,
        "stir": trust.parse_stir(verstat),
        "trust_verdict": journal.get("trust_verdict") or "screen",
        "transcript": [{"role": t.get("role"), "text": t["text"], "at": t.get("at")} for t in turns],
        "transcript_text": "\n".join(lines),
        "audio_path": None,
        "audio_bytes": 0,
        "hangup": {k: v for k, v in hangup.items() if k != "note"},
        "early_hangup": bool(hangup.get("early")),
        "state": "final",
        "ha_post": {},
    }


async def persist_fallback(session) -> dict:
    """persist_call raised (disk, audio, anything): still ship the transcript."""
    record = _base_record(session.call_id, session.reason, session.journal(), session.hangup or {})
    try:
        started = dt.datetime.fromisoformat(session.started_at)
        record["duration_s"] = round(max(0.0, time.time() - started.timestamp()), 2)
    except ValueError:
        pass
    record["persist_error"] = True
    write_json_atomic(sidecar_path(session.app, session.call_id), record)
    try:
        await deliver_webhooks(session.app, record)
    finally:
        _remember_index(session.app, record)
    return record


async def recover_orphans(app, min_age_s: float = 0.0):
    """Finalize calls journaled as open with no live session (crash/restart)."""
    recovered = 0
    for path in sorted(voicemail_dir(app).glob("*.json")):
        if path.name == "index.json":
            continue
        call_id = safe_call_id(path.stem)
        if not call_id or call_id in app["sessions"] or call_id in _inflight(app):
            continue
        journal = load_sidecar(app, call_id)
        if not journal or journal.get("state") != "open":
            continue
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            continue
        if age < min_age_s:
            continue
        caller_words = any(t.get("role") == "caller" and t.get("text") for t in journal.get("turns") or [])
        hangup = {"by": "system", "phase": "recovered", "early": True, "reason": "recovered", "note": HANGUP_NOTES["recovered"]}
        record = _base_record(call_id, "recovered", journal, hangup)
        record["recovered"] = True
        record["caller_words"] = caller_words
        write_json_atomic(sidecar_path(app, call_id), record)
        await deliver_webhooks(app, record)
        _remember_index(app, record)
        recovered += 1
        log.info("recovered open call call_id=%s", call_id)
    return recovered


def _remember_index(app, record: dict):
    targets = record.get("ha_post") or {}
    if targets.get("skipped"):
        ha_ok = True
    else:
        rows = [v for v in targets.values() if isinstance(v, dict)]
        ha_ok = bool(rows) and all(v.get("ok") for v in rows)
    update_index(app, {
        "call_id": record["call_id"],
        "ended_at": record.get("ended_at"),
        "from": mask_phone(record.get("from")),
        "duration_s": record.get("duration_s"),
        "audio": bool(record.get("audio_path")),
        "bridge": record.get("xai_bridge"),
        "stir": (record.get("stir") or {}).get("status", ""),
        "trust": record.get("trust_verdict") or "",
        "ha_ok": ha_ok,
        "ack_timeout": bool(record.get("ack_timeout")),
        "posted_unready": any(isinstance(v, dict) and v.get("posted_unready") for v in targets.values()),
    })


def webhook_body(record: dict, include_audio: bool, opts: Opts) -> dict:
    skip = {"ha_post", "_caller_wav", "audio_base64", "audio_base64_omitted", "next_retry_at", "ack_timeout"}
    body = {k: v for k, v in record.items() if k not in skip}
    if include_audio and opts.include_audio_base64:
        wav = record.get("_caller_wav") or b""
        if not wav and record.get("audio_path"):
            try:
                wav = pathlib.Path(record["audio_path"]).read_bytes()
            except OSError:
                wav = b""
        b64, omitted = maybe_audio_b64(wav, True)
        if b64:
            body["audio_base64"] = b64
        elif omitted:
            body["audio_base64_omitted"] = omitted
    return body


def ha_probe_applies(url: str) -> bool:
    """Only Home Assistant webhook URLs (``/api/webhook/<id>``) are probed.
    Nabu Casa cloudhooks and non-HA targets are posted without a probe."""
    try:
        return "/api/webhook/" in urllib.parse.urlsplit(url).path
    except ValueError:
        return False


def _ha_unregistered_reply(resp, body: bytes) -> bool:
    """True only for what HA's webhook view sends for an unknown id: a 200
    with an empty body and aiohttp's default content type (or none). Anything
    else (a body, JSON/HTML, a redirect target) is not HA's reply (L2)."""
    if resp.status != 200 or body:
        return False
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return ctype in ("", "application/octet-stream")


async def ha_probe(app, url: str) -> str:
    """GET the HA webhook with no body and no token: ``registered`` (405, the
    webhook exists and allows POST only), ``unregistered`` (empty 200, HA is
    up but the automation is not loaded, as during a Core restart),
    ``unsupported`` (a 200 that is not HA's unregistered reply, so the probe
    cannot be trusted: post without holding) or ``down``.

    The target must be POST-only (the default for HA webhook triggers): a
    GET-enabled hook would run its automation on every probe. HA checks the
    method before running a POST-only automation, so the GET never fires it.
    Read-only, no Core API, no Supervisor token. The URL (it holds the
    webhook id) is never logged."""
    try:
        timeout = aiohttp.ClientTimeout(total=HA_PROBE_TIMEOUT_S)
        async with app["client"].get(
            url,
            headers={"User-Agent": f"twilio-house-voice/{VERSION} ready-probe"},
            timeout=timeout,
            allow_redirects=False,
        ) as resp:
            if resp.status == 405:
                return "registered"
            if resp.status == 200:
                body = await resp.content.read(1)
                return "unregistered" if _ha_unregistered_reply(resp, body) else "unsupported"
            return "down"
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - any failure means not ready
        return "down"


def _ha_state(app, url: str) -> dict:
    return app["runtime"].ha_ready.setdefault(
        url, {"unready_seen": False, "ok_since": None, "confirmed_at": None}
    )


def ha_mark_post(app, url: str, ok: bool, now=None):
    """Record a post result: a 2xx refreshes the ready cache, a failure clears
    it so the next delivery probes first."""
    if not ha_probe_applies(url):
        return
    state = _ha_state(app, url)
    state["confirmed_at"] = (time.monotonic() if now is None else now) if ok else None


async def ha_ready(app, url: str, now=None):
    """(ready, why). Settle delay applies only after an unready probe and is
    measured from the first registered probe that followed it. A URL confirmed
    ready within HA_READY_TTL_S is not re-probed (each probe of a registered
    hook logs an HA warning with the webhook id and source IP)."""
    if not ha_probe_applies(url):
        return True, "not_probed"
    now = time.monotonic() if now is None else now
    state = _ha_state(app, url)
    confirmed = state.get("confirmed_at")
    if not state["unready_seen"] and confirmed is not None and now - confirmed < HA_READY_TTL_S:
        return True, "cached"
    result = await ha_probe(app, url)
    if result == "unsupported":
        state["confirmed_at"] = None
        return True, "probe_unsupported"
    if result != "registered":
        state["unready_seen"] = True
        state["ok_since"] = None
        state["confirmed_at"] = None
        return False, result
    if state["ok_since"] is None:
        state["ok_since"] = now
    if state["unready_seen"]:
        if now - state["ok_since"] < HA_SETTLE_S:
            return False, "settling"
        state["unready_seen"] = False
    state["confirmed_at"] = now
    return True, "ready"


def _hold_delay(holds: int) -> int:
    return HA_HOLD_BACKOFF_S[min(max(holds, 1), len(HA_HOLD_BACKOFF_S)) - 1]


def _inflight(app) -> set:
    """CallSids whose webhook delivery is running right now. retry_due,
    recover_orphans and record_missed skip these so one CallSid is never
    posted twice concurrently (slow webhook / HA down)."""
    return app["runtime"].inflight


async def deliver_webhooks(app, record: dict):
    call_id = record["call_id"]
    inflight = _inflight(app)
    if call_id in inflight:
        log.info("delivery already in flight call_id=%s", call_id)
        return False
    inflight.add(call_id)
    try:
        await _deliver_webhooks_impl(app, record)
    finally:
        inflight.discard(call_id)
    return True


async def _deliver_webhooks_impl(app, record: dict):
    opts = app["opts"]
    targets = webhook_targets(opts)
    if not targets:
        record["ha_post"] = {"skipped": True}
        stored = {k: v for k, v in record.items() if k != "_caller_wav"}
        write_json_atomic(sidecar_path(app, record["call_id"]), stored)
        return
    posted = app["posted"]
    held = False
    holds = 0
    results = dict(record.get("ha_post") or {})
    results.pop("skipped", None)
    # Claim the next retry slot before the first post, so a retry pass (this
    # process after a crash, or one that misses the in-flight set) does not
    # treat the record as due while this post is still waiting on HA.
    attempts_so_far = max([int(v.get("attempts") or 0) for v in results.values() if isinstance(v, dict)] or [0])
    record["next_retry_at"] = time.time() + _next_retry_delay(attempts_so_far + 1)
    write_json_atomic(sidecar_path(app, record["call_id"]), {k: v for k, v in record.items() if k != "_caller_wav"})
    for name, url, include_audio in targets:
        if not http_webhook(url):
            prev_attempts = int((results.get(name) or {}).get("attempts") or 0)
            results[name] = {"ok": False, "status": 0, "error": "bad_url", "attempts": prev_attempts + 1}
            continue
        key = (record["call_id"], url)
        previous = results.get(name) or {}
        if previous.get("ok") or key in posted:
            results[name] = previous or {"ok": True, "status": 200, "attempts": previous.get("attempts", 1)}
            posted.add(key)
            continue
        attempts = int(previous.get("attempts") or 0)
        posted_unready = False
        ready, why = await ha_ready(app, url)
        if not ready:
            held_since = float(previous.get("unready_since") or 0) or time.time()
            if time.time() - held_since < HA_UNREADY_MAX_S:
                # Held: HA is restarting. Not an attempt; retried shortly.
                results[name] = {
                    "ok": False,
                    "status": 0,
                    "error": "ha_not_ready",
                    "ready": why,
                    "attempts": attempts,
                    "unready_since": held_since,
                    "holds": int(previous.get("holds") or 0) + 1,
                }
                holds = max(holds, results[name]["holds"])
                held = True
                log.info("ha webhook held call_id=%s target=%s ready=%s", record["call_id"], name, why)
                continue
            # Wedge guard: never hold a call forever on a stuck readiness signal.
            posted_unready = True
            log.warning("ha not ready for %ds, posting anyway call_id=%s target=%s ready=%s",
                        HA_UNREADY_MAX_S, record["call_id"], name, why)
        body = webhook_body(record, include_audio, opts)
        status, ok, err = await post_webhook(app, url, record["call_id"], body)
        attempts += 1
        ha_mark_post(app, url, ok)
        results[name] = {"ok": ok, "status": status, "attempts": attempts}
        if err:
            results[name]["error"] = err
        if posted_unready or previous.get("posted_unready"):
            results[name]["posted_unready"] = True
        if ok:
            posted.add(key)
        log.info(
            "ha webhook call_id=%s target=%s ok=%s status=%s",
            record["call_id"],
            name,
            ok,
            status,
        )
    record["ha_post"] = results
    rows = [v for v in results.values() if isinstance(v, dict)]
    if rows and all(v.get("ok") for v in rows):
        record.pop("next_retry_at", None)
    elif held:
        record["next_retry_at"] = time.time() + _hold_delay(holds)
    else:
        attempts = max([int(v.get("attempts") or 0) for v in rows] or [1])
        record["next_retry_at"] = time.time() + _next_retry_delay(attempts)
    stored = {k: v for k, v in record.items() if k != "_caller_wav"}
    write_json_atomic(sidecar_path(app, record["call_id"]), stored)


async def post_webhook(app, url: str, call_id: str, body: dict):
    opts = app["opts"]
    headers = {
        "X-Idempotency-Key": call_id,
        "User-Agent": f"twilio-house-voice/{VERSION}",
    }
    payload = body
    if opts.ha_webhook_token:
        headers["Authorization"] = f"Bearer {opts.ha_webhook_token}"
        headers["X-Webhook-Token"] = opts.ha_webhook_token
        # Home Assistant webhook triggers cannot read HTTP headers. The house
        # automations require the shared secret in the JSON field `token`.
        # Copy so the secret is not written into the voicemail sidecar.
        payload = dict(body)
        payload["token"] = opts.ha_webhook_token
    delay = 0.4
    last_status = 0
    last_err = ""
    for _try in range(3):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with app["client"].post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            ) as resp:
                last_status = resp.status
                if 200 <= resp.status < 300 or resp.status == 409:
                    return resp.status, True, ""
                if 400 <= resp.status < 500:
                    return resp.status, False, "http"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_err = type(exc).__name__
            last_status = 0
        await asyncio.sleep(delay)
        delay *= 2
    return last_status, False, last_err or "http"


def _next_retry_delay(attempts: int) -> float:
    idx = min(max(0, attempts - 1), len(OUTBOX_BACKOFF_S) - 1)
    return float(OUTBOX_BACKOFF_S[idx])


def _pending_record(record: dict) -> bool:
    if record.get("ack_timeout"):
        return False  # terminal: gave up (attempt cap or max age)
    if record.get("state") == "open":
        return False  # a live call or an orphan; recover_orphans owns it
    posts = record.get("ha_post") or {}
    if posts.get("skipped"):
        return False
    rows = [v for v in posts.values() if isinstance(v, dict)]
    return not rows or not all(v.get("ok") for v in rows)


async def retry_due(app, now=None):
    """Durable outbox. Every finished call's sidecar is the outbox entry, so
    a crash between the sidecar write and the index write still retries."""
    now = time.time() if now is None else now
    for path in sorted(voicemail_dir(app).glob("*.json")):
        if path.name == "index.json":
            continue
        call_id = safe_call_id(path.stem)
        if not call_id or call_id in app["sessions"] or call_id in _inflight(app):
            continue
        try:
            record = load_sidecar(app, call_id)
            if not record or not _pending_record(record):
                continue
            try:
                ended = dt.datetime.fromisoformat(str(record.get("ended_at") or "")).timestamp()
            except ValueError:
                ended = path.stat().st_mtime
            posts = record.get("ha_post") or {}
            attempts = max([int(v.get("attempts") or 0) for v in posts.values() if isinstance(v, dict)] or [0])
            if now - ended > OUTBOX_MAX_AGE_S or attempts >= WEBHOOK_ATTEMPT_CAP:
                mark_ack_timeout(app, record, "max_age" if now - ended > OUTBOX_MAX_AGE_S else "attempt_cap", attempts)
                continue
            due = float(record.get("next_retry_at") or 0)
            if due and now < due:
                continue
            await deliver_webhooks(app, record)
            _remember_index(app, record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the outbox
            log.warning("outbox retry failed call_id=%s err=%s", call_id, type(exc).__name__)


def mark_ack_timeout(app, record: dict, reason: str, attempts: int):
    """Terminal give-up: flag the sidecar once so status shows the loss and
    the outbox never re-posts it."""
    record["ack_timeout"] = {"reason": reason, "attempts": attempts, "at": iso_now()}
    record.pop("next_retry_at", None)
    write_json_atomic(sidecar_path(app, record["call_id"]), {k: v for k, v in record.items() if k != "_caller_wav"})
    _remember_index(app, record)
    log.warning("ha delivery gave up call_id=%s reason=%s attempts=%s", record["call_id"], reason, attempts)


async def webhook_retry_loop(app):
    while True:
        try:
            await asyncio.sleep(WEBHOOK_RETRY_S)
            await retry_due(app)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            log.warning("outbox loop error err=%s", type(exc).__name__)


async def watchdog_tick(app, now=None):
    """Finalize stuck calls and recover orphans. Returns finalized call ids."""
    now = time.monotonic() if now is None else now
    done = []
    for session in list(app["sessions"].values()):
        if session.finished:
            continue
        ws = session.twilio_ws
        why = None
        if ws is None or ws.closed:
            if now - session.last_frame > WATCHDOG_SOCKET_GONE_S:
                why = "socket_gone"
        if why is None and now - session.last_frame > WATCHDOG_SILENT_S:
            why = "silent"
        if why is None and session.status_terminal_at is not None and now - session.status_terminal_at > WATCHDOG_STATUS_GRACE_S:
            why = "status_terminal"
        if why is None and time.time() - session.started > WATCHDOG_MAX_CALL_S:
            why = "max_call"
        if why:
            log.warning("watchdog finalize call_id=%s why=%s", session.call_id, why)
            await session.finalize("watchdog:" + why)
            if ws is not None and not ws.closed:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
            done.append(session.call_id)
    await recover_orphans(app, min_age_s=WATCHDOG_ORPHAN_S)
    rt = app["runtime"]
    if rt.last_prune is None or time.time() - rt.last_prune >= SIDECAR_PRUNE_EVERY_S:
        rt.last_prune = time.time()
        prune_sidecars(app)
    return done


def _acked(record: dict) -> bool:
    posts = record.get("ha_post") or {}
    if posts.get("skipped"):
        return True
    rows = [v for v in posts.values() if isinstance(v, dict)]
    return bool(rows) and all(v.get("ok") for v in rows)


def prune_sidecars(app, now=None, max_age_s=None) -> int:
    """Delete final, acknowledged sidecars (and their audio) past retention.
    Open journals and unacked outbox entries are never pruned."""
    now = time.time() if now is None else now
    max_age_s = SIDECAR_RETENTION_S if max_age_s is None else max_age_s
    directory = voicemail_dir(app)
    removed = 0
    for path in sorted(directory.glob("*.json")):
        if path.name == "index.json":
            continue
        call_id = safe_call_id(path.stem)
        if not call_id or call_id in app["sessions"] or call_id in _inflight(app):
            continue
        try:
            if now - path.stat().st_mtime < max_age_s:
                continue
        except OSError:
            continue
        record = load_sidecar(app, call_id)
        if not record or record.get("state") != "final" or not _acked(record):
            continue
        for extra in (directory / f"{call_id}.wav", directory / f"{call_id}.assistant.wav"):
            try:
                extra.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("pruned %s acknowledged sidecars", removed)
    return removed


async def watchdog_loop(app):
    while True:
        try:
            await asyncio.sleep(WATCHDOG_TICK_S)
            await watchdog_tick(app)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("watchdog error err=%s", type(exc).__name__)


async def record_missed(app, call_id: str, params: dict, reason: str):
    """A terminal status with no media session still notifies HA.

    One record per CallSid: a finished call is only re-delivered if HA has
    not acknowledged it yet, and a journaled open call is recovered rather
    than replaced by an empty missed-call record.
    """
    if call_id in _inflight(app):
        return
    existing = load_sidecar(app, call_id)
    if existing and existing.get("state") == "open":
        await recover_orphans(app)
        return
    if existing:
        if _pending_record(existing):
            await deliver_webhooks(app, existing)
            _remember_index(app, existing)
        return
    now = iso_now()
    meta = app["call_meta"].get(call_id) or {}
    verstat = trust.clean_verstat(params.get("StirVerstat") or meta.get("stir_verstat"))
    record = {
        "addon": "twilio_house_voice",
        "version": VERSION,
        "call_id": call_id,
        "event": "hangup",
        "reason": reason,
        "from": params.get("From") or "",
        "to": params.get("To") or "",
        "direction": params.get("Direction") or "",
        "started_at": now,
        "ended_at": now,
        "duration_s": 0,
        "audio_seconds": 0,
        "xai_bridge": app["opts"].bridge_mode(),
        "xai_connected": False,
        "stir_verstat": verstat,
        "stir": trust.parse_stir(verstat),
        "trust_verdict": meta.get("trust_verdict") or "screen",
        "transcript": [],
        "transcript_text": "",
        "audio_path": None,
        "audio_bytes": 0,
        "hangup": {"by": "caller", "phase": "before_stream", "early": True, "reason": reason},
        "early_hangup": True,
        "state": "final",
        "ha_post": {},
    }
    write_json_atomic(sidecar_path(app, call_id), record)
    await deliver_webhooks(app, record)
    _remember_index(app, record)
    log.info("missed call recorded call_id=%s reason=%s", call_id, reason)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def in_ha() -> bool:
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


def ingress_blocked(path: str, remote, supervisor: bool) -> bool:
    if path == "/api/health" or path.startswith("/twilio/") or path.startswith("/api/trust"):
        # /api/trust checks its own credentials (see trust_authorized).
        return False
    if supervisor and remote not in INGRESS_PEERS:
        return True
    return False


CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "object-src 'none'",
        "frame-ancestors 'self'",
    ]
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "SAMEORIGIN",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}


def _harden(request, resp):
    if getattr(resp, "prepared", False):
        return resp  # WebSocket / streamed: headers already sent
    for key, value in SECURITY_HEADERS.items():
        resp.headers.setdefault(key, value)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@web.middleware
async def gate(request, handler):
    if ingress_blocked(request.path, request.remote, in_ha()):
        return _harden(request, web.Response(status=403, text="ingress only"))
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        _harden(request, exc)
        raise
    except Exception as exc:  # noqa: BLE001 - unhandled crash: 500 with headers, no details
        log.error("unhandled error on %s: %s", request.path, type(exc).__name__)
        resp = web.json_response({"ok": False, "error": "internal"}, status=500)
    return _harden(request, resp)


def mint_token(app, call_id: str, params: dict) -> str:
    now = time.time()
    stale = [k for k, v in app["tokens"].items() if v["exp"] < now]
    for key in stale:
        app["tokens"].pop(key, None)
    token = secrets.token_urlsafe(24)
    app["tokens"][token] = {
        "call_id": call_id,
        "exp": now + TOKEN_TTL_S,
        "from": params.get("From") or "",
        "to": params.get("To") or "",
        "direction": params.get("Direction") or "",
        "stir_verstat": trust.clean_verstat(params.get("StirVerstat")),
        "trust_verdict": params.get("_trust_verdict") or "screen",
    }
    return token


def consume_token(app, token: str):
    item = app["tokens"].pop(token, None)
    if not item or item["exp"] < time.time():
        return None
    return item


REJECT_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response><Reject reason="rejected"/></Response>'


def remember_call_meta(app, call_id: str, verstat: str, verdict: str):
    meta = app["call_meta"]
    meta[call_id] = {"stir_verstat": verstat, "trust_verdict": verdict}
    while len(meta) > CALL_META_CAP:
        meta.pop(next(iter(meta)))


def trust_authorized(request) -> bool:
    """Trust lists hold full phone numbers.

    Accepted: the Supervisor ingress proxy with an HA user header (this
    add-on's own admin page), or the trust_api_token bearer from a peer on
    the Supervisor network or loopback (CallCatch). Nothing else, and never
    an empty token.
    """
    remote = request.remote or ""
    user = (request.headers.get("X-Remote-User-Id") or request.headers.get("X-Hass-User-ID") or "").strip()
    if remote in INGRESS_PEERS and user:
        return True
    token = request.app["opts"].trust_api_token
    auth = request.headers.get("Authorization") or ""
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token or not presented:
        return False
    if remote == DOCKER_GATEWAY or not (
        remote.startswith(SUPERVISOR_NET_PREFIX) or remote in ("127.0.0.1", "::1")
    ):
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode()).digest(), hashlib.sha256(token.encode()).digest()
    )


def _trust_denied():
    return web.json_response({"ok": False, "error": "forbidden"}, status=403)


async def api_trust_get(request):
    if not trust_authorized(request):
        return _trust_denied()
    snap = request.app["trust"].snapshot()
    return web.json_response({"ok": True, **snap, "blocked_calls": request.app["stats"].get("blocked", 0)})


async def _trust_body(request):
    try:
        body = await request.json()
    except (json.JSONDecodeError, aiohttp.ContentTypeError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


async def api_trust_add(request):
    if not trust_authorized(request):
        return _trust_denied()
    body = await _trust_body(request)
    if body is None:
        return web.json_response({"ok": False, "error": "bad-request"}, status=400)
    try:
        entry, previous = request.app["trust"].add(
            str(body.get("list") or ""), body.get("number"), str(body.get("note") or "")
        )
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    log.info("trust add list=%s number=%s previous=%s", body.get("list"), mask_phone(entry["number"]), previous or "-")
    return web.json_response({"ok": True, "list": body.get("list"), "entry": entry, "previous": previous})


async def api_trust_remove(request):
    if not trust_authorized(request):
        return _trust_denied()
    body = await _trust_body(request)
    if body is None:
        return web.json_response({"ok": False, "error": "bad-request"}, status=400)
    try:
        removed = request.app["trust"].remove(str(body.get("list") or ""), body.get("number"))
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    log.info("trust remove list=%s number=%s removed=%s", body.get("list"), mask_phone(body.get("number")), removed)
    return web.json_response({"ok": True, "removed": removed})


# --------------------------------------------------------------------------
# stdin commands (hassio.addon_stdin): the Home Assistant route used by the
# CallCatch notification's Block / Undo buttons. No add-on calls this.
# --------------------------------------------------------------------------

STDIN_COMMANDS = {"block": ("add", "block"), "unblock": ("remove", "block")}


def _stdin_values(buffer: str):
    """Yield complete JSON values from a stdin buffer; return the remainder.

    Supervisor writes the service `input` as JSON, with or without a trailing
    newline, so values are decoded back to back rather than by line.
    """
    decoder = json.JSONDecoder()
    values = []
    idx = 0
    while True:
        while idx < len(buffer) and buffer[idx] in " \t\r\n":
            idx += 1
        if idx >= len(buffer):
            return values, ""
        try:
            value, end = decoder.raw_decode(buffer, idx)
        except json.JSONDecodeError:
            if len(buffer) - idx > 4096:
                return values, ""  # garbage: drop it rather than grow forever
            return values, buffer[idx:]
        values.append(value)
        idx = end


def handle_stdin_command(app, value) -> dict:
    """Apply one {cmd, number, note?, dry_run?} command. Returns a result dict."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {"ok": False, "error": "bad-json"}
    if not isinstance(value, dict):
        return {"ok": False, "error": "bad-command"}
    cmd = str(value.get("cmd") or "")
    if cmd not in STDIN_COMMANDS:
        log.warning("stdin unknown cmd")
        return {"ok": False, "error": "unknown-cmd"}
    op, name = STDIN_COMMANDS[cmd]
    raw = value.get("number")
    # HA templates can render a number as an int; never let a type crash us.
    number = str(raw) if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
    if not trust.number_key(number):
        log.warning("stdin %s without a usable number", cmd)
        return {"ok": False, "error": "bad-number"}
    if _as_bool(value, "dry_run", False):
        log.info("stdin dry_run %s number=%s (no change)", cmd, mask_phone(number))
        return {"ok": True, "dry_run": True, "cmd": cmd}
    try:
        if op == "add":
            entry, previous = app["trust"].add(name, number, str(value.get("note") or "notification")[:80])
            log.info("stdin block number=%s previous=%s", mask_phone(entry["number"]), previous or "-")
            return {"ok": True, "cmd": cmd, "previous": previous}
        removed = app["trust"].remove(name, number)
        log.info("stdin unblock number=%s removed=%s", mask_phone(number), removed)
        return {"ok": True, "cmd": cmd, "removed": removed}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


async def stdin_loop(app, stream=None):
    stream = stream or sys.stdin
    loop = asyncio.get_running_loop()
    buffer = ""
    while True:
        chunk = await loop.run_in_executor(None, stream.readline)
        if not chunk:
            return
        buffer += chunk
        values, buffer = _stdin_values(buffer)
        for value in values:
            try:
                handle_stdin_command(app, value)
            except Exception as exc:  # noqa: BLE001 - one bad command must not stop the listener
                log.warning("stdin command failed: %s", type(exc).__name__)


def stdin_enabled() -> bool:
    if os.environ.get("THV_STDIN", "") == "0" or not in_ha():
        return False
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def read_form(request):
    form = await request.post()
    return {k: str(v) for k, v in form.items()}


async def twilio_voice(request):
    opts = request.app["opts"]
    if twilio_unavailable(opts):
        log.warning("voice webhook refused: twilio not configured")
        return web.Response(status=503, text="twilio not configured")
    params = await read_form(request)
    url = signature_url(request, opts)
    if not twilio_authorized(opts, request.remote, url, params, request.headers.get("X-Twilio-Signature", "")):
        log.warning("voice webhook rejected")
        return web.Response(status=403, text="forbidden")
    call_id = safe_call_id(params.get("CallSid") or "")
    if not call_id:
        return web.Response(status=400, text="missing CallSid")
    verstat = trust.clean_verstat(params.get("StirVerstat"))
    verdict = trust.classify(request.app["trust"], params.get("From"))
    stir = trust.parse_stir(verstat)
    if trust.block_action(verdict, stir) == "screen":
        verdict = "blocked"  # screened, tagged; not rejected (see DOCS: Trust gate)
    remember_call_meta(request.app, call_id, verstat, verdict)
    if verdict == "block":
        request.app["stats"]["blocked"] = request.app["stats"].get("blocked", 0) + 1
        log.info("voice call_id=%s from=%s blocked stir=%s", call_id, mask_phone(params.get("From")), stir["status"])
        return web.Response(text=REJECT_TWIML, content_type="text/xml")
    params = dict(params, _trust_verdict=verdict)
    token = mint_token(request.app, call_id, params)
    base = public_http_base(request, opts)
    xml = build_twiml(base, token, call_id, speak_fallback=opts.bridge_mode() == "stub")
    log.info(
        "voice call_id=%s from=%s trust=%s stir=%s%s",
        call_id, mask_phone(params.get("From")), verdict, stir["status"], stir["level"],
    )
    return web.Response(text=xml, content_type="text/xml")


async def twilio_status(request):
    opts = request.app["opts"]
    if twilio_unavailable(opts):
        log.warning("status webhook refused: twilio not configured")
        return web.Response(status=503, text="twilio not configured")
    params = await read_form(request)
    url = signature_url(request, opts)
    if not twilio_authorized(opts, request.remote, url, params, request.headers.get("X-Twilio-Signature", "")):
        log.warning("status webhook rejected")
        return web.Response(status=403, text="forbidden")
    call_id = safe_call_id(params.get("CallSid") or "")
    if not call_id:
        return web.Response(status=400, text="missing CallSid")
    status = params.get("CallStatus") or ""
    if status and status not in TERMINAL_STATUS:
        return web.Response(text="ok")
    reason = "status:" + (status or "connect")
    meta = request.app["call_meta"].get(call_id) or {}
    if meta.get("trust_verdict") == "block":
        # A rejected call is not a voicemail: no sidecar, no webhook, no inbox row.
        return web.Response(text="ok")
    session = request.app["sessions"].get(call_id)
    if session and not session.finished:
        if params.get("From") and not session.peer_from:
            session.peer_from = params.get("From") or ""
        if params.get("To") and not session.peer_to:
            session.peer_to = params.get("To") or ""
        # An open Media Stream owns the hangup so the WAV includes the tail
        # of the call. Status is the backup when that socket is already gone.
        ws = session.twilio_ws
        if ws is not None and not ws.closed:
            # The watchdog finalizes if the socket never delivers `stop`.
            if session.status_terminal_at is None:
                session.status_terminal_at = time.monotonic()
            return web.Response(text="ok")
        await session.finalize(reason)
    elif session and session.finished:
        return web.Response(text="ok")
    else:
        await record_missed(request.app, call_id, params, reason)
    return web.Response(text="ok")


def start_custom_parameters(event: dict) -> dict:
    start = event.get("start")
    if not isinstance(start, dict):
        return {}
    params = start.get("customParameters")
    if not isinstance(params, dict):
        return {}
    return params


async def read_authorized_start(app, ws):
    """Wait for the Media Streams start event and consume its one-time token.

    The WebSocket is already upgraded. Twilio's first frame is `connected`;
    `start` follows and carries <Parameter> values on customParameters.
    The deadline is from upgrade, not from the latest frame, so junk frames
    cannot hold the socket open. The first start event is the only chance:
    a bad token closes the socket instead of waiting for another one.

    Returns (pending, start_event) or (None, None). Does not log the token.
    """
    deadline = time.monotonic() + STREAM_START_WAIT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("stream start timeout")
            return None, None
        try:
            msg = await ws.receive(remaining)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("stream start timeout")
            return None, None
        if msg.type != aiohttp.WSMsgType.TEXT:
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.ERROR,
            ):
                return None, None
            continue
        try:
            event = json.loads(msg.data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "start":
            continue
        token = str(start_custom_parameters(event).get("token") or "").strip()
        pending = consume_token(app, token)
        if not pending:
            log.warning("stream token rejected")
            return None, None
        return pending, event


async def reject_stream(ws):
    if not ws.prepared or ws.closed:
        return
    try:
        await ws.close(code=STREAM_REJECT_CODE, message=b"forbidden")
    except Exception:
        return


async def twilio_stream(request):
    """Media Streams socket.

    A query token is ignored on purpose. <Stream> does not deliver one
    (Twilio 31920), and accepting it here would split auth across two paths.
    Non-WebSocket requests stay 403. A real upgrade is accepted first; the
    one-time token is then read from the start event.
    """
    if twilio_unavailable(request.app["opts"]):
        return web.Response(status=503, text="twilio not configured")
    ws = web.WebSocketResponse(heartbeat=25)
    if not ws.can_prepare(request):
        return web.Response(status=403, text="forbidden")
    await ws.prepare(request)
    pending, start_event = await read_authorized_start(request.app, ws)
    if pending is None:
        await reject_stream(ws)
        return ws
    call_id = pending["call_id"]
    existing = request.app["sessions"].get(call_id)
    if existing and not existing.finished:
        existing.replaced += 1
        old = existing.twilio_ws
        existing.twilio_ws = ws
        session = existing
        if old is not None and not old.closed:
            await old.close()
        log.info("stream replaced call_id=%s", call_id)
    else:
        session = CallSession(request.app, call_id, pending)
        session.twilio_ws = ws
        request.app["sessions"][call_id] = session
        session.checkpoint()
        log.info("stream open call_id=%s bridge=%s", call_id, session.bridge)
    try:
        if start_event is not None:
            await session.on_twilio(start_event)
        if session.finished:
            return ws
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    await session.on_twilio(event)
                if session.finished:
                    break
            elif msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
                aiohttp.WSMsgType.CLOSE,
            ):
                break
    finally:
        if session.twilio_ws is ws:
            await session.finalize("disconnect")
    return ws


async def api_health(_request):
    return web.json_response({"ok": True})


async def api_status(request):
    opts = request.app["opts"]
    active = []
    for session in request.app["sessions"].values():
        active.append({
            "call_id": session.call_id,
            "started_at": session.started_at,
            "from": mask_phone(session.peer_from),
            "bridge": session.bridge,
            "xai_connected": session.xai_connected,
        })
    return web.json_response({
        "ok": True,
        "version": VERSION,
        "bridge": opts.bridge_mode(),
        "bridge_setting": opts.xai_bridge,
        "twilio_configured": bool(opts.twilio_account_sid and opts.twilio_auth_token),
        "xai_configured": bool(opts.xai_api_key),
        "public_base_url_set": bool(opts.public_base_url),
        "webhooks_configured": bool(webhook_targets(opts)),
        "signature_validation": bool(opts.twilio_auth_token),
        "phone_masked": mask_phone(opts.twilio_phone_number),
        "voice": opts.xai_voice,
        "model": opts.xai_model,
        "mix_recording": opts.mix_recording,
        "active_calls": active,
        "calls_handled": request.app["stats"].get("calls", 0),
        "blocked_calls": request.app["stats"].get("blocked", 0),
        "trust_api_configured": bool(opts.trust_api_token),
        "trust_counts": {k: len(v) for k, v in request.app["trust"].snapshot().items()},
        "recent": load_index(request.app)[:20],
        "voicemail_dir": str(voicemail_dir(request.app)),
    })


async def index(_request):
    page = pathlib.Path(__file__).resolve().parent / "static" / "index.html"
    return web.FileResponse(page, headers={"Cache-Control": "no-store"})


async def method_not_allowed(_request):
    return web.Response(status=405, text="Twilio must POST this URL")


async def on_startup(app):
    app["client"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
    )
    voicemail_dir(app)
    try:
        await recover_orphans(app)
    except Exception as exc:  # noqa: BLE001
        log.warning("startup recovery failed err=%s", type(exc).__name__)
    app["retry_task"] = asyncio.create_task(webhook_retry_loop(app))
    app["watchdog_task"] = asyncio.create_task(watchdog_loop(app))
    if stdin_enabled():
        app["stdin_task"] = asyncio.create_task(stdin_loop(app))


async def on_cleanup(app):
    stdin_task = app.get("stdin_task")
    if stdin_task:
        stdin_task.cancel()
    for name in ("retry_task", "watchdog_task"):
        task = app.get(name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    for session in list(app["sessions"].values()):
        await session.finalize("shutdown")
    pending = [t for t in app.get("finalizing", set()) if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=15)
    client = app.get("client")
    if client and not client.closed:
        await client.close()


def create_app(options=None, data_dir=None):
    opts = Opts(load_options() if options is None else options)
    setup_logging(opts.log_level, opts.secret_values())
    if opts.twilio_auth_token and not opts.validate_twilio_signature:
        log.warning("validate_twilio_signature is off but is ignored: Twilio signatures are always checked when an auth token is set")
    app = web.Application(middlewares=[gate], client_max_size=1_048_576)
    app["opts"] = opts
    app["data_dir"] = pathlib.Path(data_dir or os.environ.get("THV_DATA", "/data"))
    app["sessions"] = {}
    app["tokens"] = {}
    app["posted"] = set()
    app["stats"] = {"calls": 0, "last": None, "blocked": 0}
    app["call_meta"] = {}
    # Mutable runtime state lives in one holder created before startup, so
    # nothing assigns app[...] after the app is frozen (aiohttp deprecation).
    app["runtime"] = types.SimpleNamespace(
        last_prune=None,  # time.time() of the last sidecar prune
        inflight=set(),  # CallSids whose delivery is running now
        ha_ready={},  # webhook url -> {"unready_seen", "ok_since", "confirmed_at"}
    )
    app["finalizing"] = set()
    app["trust"] = trust.TrustLists(app["data_dir"] / "trust_lists.json")
    app.router.add_get("/", index)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/twilio/voice", twilio_voice)
    app.router.add_get("/twilio/voice", method_not_allowed)
    app.router.add_post("/twilio/status", twilio_status)
    app.router.add_get("/twilio/stream", twilio_stream)
    app.router.add_get("/api/trust", api_trust_get)
    app.router.add_post("/api/trust/add", api_trust_add)
    app.router.add_post("/api/trust/remove", api_trust_remove)
    static = pathlib.Path(__file__).resolve().parent / "static"
    app.router.add_static("/static/", static, append_version=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


# Store migration (plan C3): allow-listed /data files staged in
# /share/.migrate-2026-11-01/twilio_house_voice/ by the owner. Trust lists
# move file-to-file (C6). Destinations are relative to /data.
SHARE_IMPORT_ALLOW = {
    "trust_lists.json": "trust_lists.json",
    "index.json": "voicemail/index.json",
}


def run_share_import(data_dir=None) -> dict:
    """One-shot import before the app opens /data. Never raises."""
    root = pathlib.Path(data_dir or os.environ.get("THV_DATA", "/data"))
    try:
        return share_import.import_from_share("twilio_house_voice", root, SHARE_IMPORT_ALLOW)
    except Exception as exc:  # noqa: BLE001 - import must never stop the bridge
        log.error("import_from_share failed: %s", type(exc).__name__)
        return {"status": "error"}


def main():
    run_share_import()
    app = create_app()
    opts = app["opts"]
    problem = startup_config_error(opts)
    if problem:
        log.error("CallCatch Voice %s refusing to start: %s", VERSION, problem)
        raise SystemExit(2)
    log.info(
        "CallCatch Voice %s starting bridge=%s twilio=%s xai=%s public_base=%s webhooks=%s port=%s",
        VERSION,
        opts.bridge_mode(),
        "set" if opts.twilio_auth_token else "missing",
        "set" if opts.xai_api_key else "missing",
        "set" if opts.public_base_url else "missing",
        "set" if webhook_targets(opts) else "missing",
        PORT,
    )
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
