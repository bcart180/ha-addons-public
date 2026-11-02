#!/usr/bin/env python3
"""CallCatch: Your Personalized Voicemail, behind Supervisor ingress.

The inbox lives in this App's /data store. CallCatch Voice posts transcripts
and audio to /api/ingest with the configured bearer token. Clear, mark heard,
and playback run here. sensor.inbound_voicemails is published for dashboards.

legacy_pyscript=auto keeps the pyscript services when the legacy JSON file is
present, so an existing install keeps working. Slug stays voicemail.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import pathlib
import re
import sys
import tempfile
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

import call_summary
import drive_backend
import inbox_store
import legacy_import
import share_import

APP_DIR = pathlib.Path(__file__).resolve().parent
STATIC = APP_DIR / "static"
EXAMPLE = APP_DIR / "example_voicemails.json"
VERSION = "0.4.3"
AUDIO_LIMIT = 25 * 1024 * 1024
SENSOR_RE = re.compile(r"sensor\.[a-z0-9_]+")
RT = web.AppKey("rt", dict)

OPTIONS_PATH = os.environ.get("VM_OPTIONS", "/data/options.json")


def load_options() -> dict:
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


OPTS = load_options()
PORT = int(os.environ.get("VM_PORT", "8099"))
MOCK = bool(OPTS.get("mock")) or os.environ.get("VM_MOCK") == "1"
SENSOR = (OPTS.get("sensor_entity") or "sensor.inbound_voicemails").strip()
# Blank unless the user sets one. No built-in speaker name.
MEDIA_PLAYER = (OPTS.get("media_player") or "").strip()
_SECRETS: list[str] = []


def remember_secret(value: Any) -> None:
    text = str(value or "")
    if len(text) >= 8 and text not in _SECRETS:
        _SECRETS.append(text)


for _secret_key in (
    "ingest_token",
    "google_service_account_json",
    "google_oauth_client_secret",
    "google_oauth_refresh_token",
    "voice_trust_token",
):
    remember_secret(OPTS.get(_secret_key))

INGEST_TOKEN = str(OPTS.get("ingest_token") or "")
# CallCatch Voice owns the allow / block lists in its /data. The inbox edits
# them through its own ingress-gated routes, which forward with this bearer
# to Voice on the Supervisor network. Blank = lists not editable here.
_TRUST_URL_RE = re.compile(r"^http://([a-z0-9][a-z0-9\-]{0,62}):(\d{2,5})/?$")


def voice_trust_base(raw: Any) -> str:
    """Only http://<addon-hostname>:<port>. No dots, so never a public host."""
    text = str(raw or "").strip()
    m = _TRUST_URL_RE.fullmatch(text)
    if not m:
        return ""
    return f"http://{m.group(1)}:{m.group(2)}"


VOICE_TRUST_BASE = voice_trust_base(OPTS.get("voice_trust_url"))
VOICE_TRUST_TOKEN = str(OPTS.get("voice_trust_token") or "")


def _token_from_s6_env(name: str) -> str:
    """S6 keeps container env under /run/s6/container_environment/NAME."""
    for base in ("/run/s6/container_environment", "/var/run/s6/container_environment"):
        path = pathlib.Path(base) / name
        try:
            val = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if val:
            return val
    return ""


def _supervisor_token() -> str:
    return (
        os.environ.get("SUPERVISOR_TOKEN")
        or os.environ.get("HASSIO_TOKEN")
        or _token_from_s6_env("SUPERVISOR_TOKEN")
        or _token_from_s6_env("HASSIO_TOKEN")
        or ""
    ).strip()


def _in_ha() -> bool:
    return bool(_supervisor_token())


IN_HA = False
_raw_allow = os.environ.get("VM_ALLOW", "172.30.0.0/16")
ALLOW = [a for a in _raw_allow.split(",") if a]
if MOCK and not _in_ha():
    ALLOW = []
SUPERVISOR_INGRESS_IP = os.environ.get("VM_INGRESS_IP", "172.30.32.2").strip() or "172.30.32.2"

logging.basicConfig(
    level=getattr(logging, str(OPTS.get("log_level", "info")).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s voicemail: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("voicemail")

# 0.4.2: drop only successful (2xx) access lines for exactly GET /api/health (Supervisor watchdog / HEALTHCHECK).
# Anchored on the full request line + status, so errors, other methods, query strings and /api/healthz still log.
# aiohttp default access format: %a %t "%r" %s ... -> match from line start so a UA/Referer
# containing the health request line cannot hide another request.
_HEALTH_OK = re.compile(r'^\S+ \[[^\]]*\] "GET /api/health HTTP/1\.[01]" 2\d\d ')


class NoHealthAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _HEALTH_OK.match(record.getMessage()) is None

PHONE_RE = re.compile(r"(?<!\d)(\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4})(?!\d)")


def redact(text: Any) -> str:
    rendered = str(text)
    tok = _supervisor_token()
    if tok and len(tok) >= 4:
        rendered = rendered.replace(tok, "***")
    for secret in _SECRETS:
        if secret and secret in rendered:
            rendered = rendered.replace(secret, "***")
    if "PRIVATE KEY" in rendered:
        return "[redacted]"
    return rendered


def digits(n: Any) -> str:
    return inbox_store.digits(n)


def tel_href(caller: Any) -> str | None:
    return inbox_store.tel_href(caller)


def data_dir() -> pathlib.Path:
    override = (os.environ.get("VM_DATA_DIR") or "").strip()
    if override:
        return pathlib.Path(override)
    default = pathlib.Path("/data/callcatch")
    parent = default.parent
    if parent.is_dir() and os.access(parent, os.W_OK):
        return default
    # Local preview has no Supervisor /data volume.
    if MOCK:
        return pathlib.Path(tempfile.gettempdir()) / "callcatch"
    return default


def audio_dir_option() -> pathlib.Path | None:
    raw = (os.environ.get("VM_AUDIO_DIR") or OPTS.get("audio_dir") or "").strip()
    if not raw or raw in (".", "/"):
        return None
    return pathlib.Path(raw)


def audio_root() -> pathlib.Path:
    """New recordings. /media/callcatch, else /share/callcatch, else /data. Never /local."""
    override = (os.environ.get("VM_AUDIO_ROOT") or "").strip()
    if override:
        return pathlib.Path(override)
    for parent in (pathlib.Path("/media"), pathlib.Path("/share")):
        if parent.is_dir() and os.access(parent, os.W_OK):
            return parent / "callcatch"
    return data_dir() / "audio"


def legacy_paths() -> list[pathlib.Path]:
    return legacy_import.legacy_candidates(os.environ.get("VM_LEGACY_PATHS", ""))


def legacy_enabled() -> bool:
    setting = OPTS.get("legacy_pyscript", "auto")
    return inbox_store.legacy_mode(setting, legacy_import.legacy_file_exists(legacy_paths()))


def storage_backend() -> str:
    val = str(OPTS.get("storage_backend") or "local").strip()
    return val if val in ("local", "google_drive") else "local"


def keep_local_copy() -> bool:
    if "keep_local_copy" not in OPTS:
        return True
    return bool(OPTS.get("keep_local_copy"))


def drive_ready() -> bool:
    if storage_backend() != "google_drive":
        return False
    if not str(OPTS.get("google_drive_folder_id") or "").strip():
        return False
    if drive_backend.parse_service_account(str(OPTS.get("google_service_account_json") or "")):
        return True
    return bool(
        str(OPTS.get("google_oauth_refresh_token") or "").strip()
        and str(OPTS.get("google_oauth_client_id") or "").strip()
        and str(OPTS.get("google_oauth_client_secret") or "").strip()
    )


def mirror_dirs() -> list[pathlib.Path]:
    paths = []
    for part in os.environ.get("VM_MIRROR_DIRS", "").split(","):
        part = part.strip()
        if part:
            paths.append(pathlib.Path(part))
    paths.extend(
        [
            pathlib.Path("/media/voicemail"),
            pathlib.Path("/share/twilio_house_voice/voicemail"),
        ]
    )
    legacy_audio = audio_dir_option()
    if legacy_audio is not None:
        paths.append(legacy_audio)
    return paths


def pretty_date_short(item: dict) -> str:
    return (
        item.get("date_display")
        or item.get("timestamp_display")
        or (str(item.get("date") or "")[:10])
        or ""
    )


# Display labels only. Saved text keeps whatever the bridge wrote (often "House:").
_SPEAKER_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<label>house|assistant|agent|caller)[ \t]*:[ \t]*(?P<rest>.*)$",
    re.IGNORECASE,
)


def map_speaker_line(line: str) -> str:
    """Map a leading speaker prefix. house/assistant/agent → Assistant, caller → Caller."""
    match = _SPEAKER_LINE.match(line)
    if not match:
        return line
    name = "Caller" if match.group("label").casefold() == "caller" else "Assistant"
    rest = match.group("rest")
    indent = match.group("indent")
    if rest:
        return f"{indent}{name}: {rest}"
    return f"{indent}{name}:"


def map_speaker_text(text: Any) -> Any:
    """Display-only speaker labels. Non-strings and stored records are not rewritten."""
    if not isinstance(text, str) or not text:
        return text
    parts = re.split(r"(\r\n|\n|\r)", text)
    return "".join(
        part if part in ("\n", "\r\n", "\r") else map_speaker_line(part) for part in parts
    )


def audio_file_for(call_id: str) -> pathlib.Path | None:
    """Read-only lookup in the legacy audio_dir option. Not a write target."""
    root = audio_dir_option()
    safe = inbox_store.safe_id(call_id)
    if not safe or root is None or not root.is_dir():
        return None
    for ext in inbox_store.AUDIO_EXTS:
        path = root / f"{safe}.{ext}"
        if path.is_file():
            return path
    return None


def normalize_item(raw: dict) -> dict:
    caller = (
        raw.get("caller_title")
        or raw.get("caller_pretty")
        or raw.get("caller_display")
        or raw.get("caller")
        or "Unknown"
    )
    number = raw.get("caller_pretty") or raw.get("caller_display") or raw.get("caller") or ""
    cid = str(raw.get("call_id") or "").strip()
    audio_url = raw.get("audio_url") or ""
    has_audio = bool(raw.get("has_audio")) or bool(audio_url) or bool(cid and audio_file_for(cid))
    snippet_raw = raw.get("snippet") or ""
    transcript_raw = raw.get("transcript") or snippet_raw or ""
    return {
        "voicemail_id": raw.get("voicemail_id") or raw.get("message_id") or cid,
        "caller_title": str(caller),
        "caller_pretty": str(number),
        "caller_digits": digits(raw.get("caller") or number),
        "tel": tel_href(raw.get("caller") or number),
        "date_display": pretty_date_short(raw),
        "timestamp_display": raw.get("timestamp_display") or pretty_date_short(raw),
        "snippet": map_speaker_text(snippet_raw[:180] if isinstance(snippet_raw, str) else snippet_raw),
        "transcript": map_speaker_text(transcript_raw),
        "heard": bool(raw.get("heard")),
        "has_audio": has_audio,
        "audio_url": audio_url,
        "call_id": cid,
        "duration_sec": int(raw.get("duration_sec") or 0),
        "duration_display": raw.get("duration_display") or "",
        "missed_call": bool(raw.get("missed_call")),
        "business": bool(raw.get("business")),
        "short_id": raw.get("short_id") or "",
        "stir": inbox_store.stir_summary(raw.get("stir_verstat")),
        "trust_verdict": raw.get("trust_verdict") or "",
        "summary": call_summary.for_display(call_summary.stored_or_extract(raw)),
    }


def read_store_file() -> dict | None:
    """Legacy read path when the Core sensor is unreachable."""
    for path in legacy_paths():
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("store %s: %s", path.name, type(exc).__name__)
            continue
        if isinstance(data, dict):
            values = list(data.values())
            items = values if values and isinstance(values[0], dict) else []

            def _key(it):
                return str(it.get("date") or it.get("detected_at") or "")

            items.sort(key=_key, reverse=True)
            return {
                "state": str(len(items)),
                "attributes": {
                    "voicemails": items,
                    "unheard": sum(1 for x in items if not x.get("heard")),
                    "source": "file",
                },
            }
    return None


MAX_STORE_BYTES = 8_000_000
MASK_CHAR = "\u2022"


def read_store_rows() -> dict[str, dict] | None:
    """Full pyscript rows keyed by voicemail_id, read-only.

    The pyscript sensor may publish redacted rows (masked numbers, no
    transcript). The full rows live in the pyscript store file, which this
    add-on already maps read-only. None means no readable store, so the
    caller falls back to the sensor rows as they are.
    """
    for path in legacy_paths():
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_STORE_BYTES:
                log.warning("store %s: too-large", path.name)
                continue
            # Read-only. Never open this path for write.
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("store %s: %s", path.name, type(exc).__name__)
            continue
        if not isinstance(data, dict):
            continue
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    return None


def merge_store_row(sensor_row: dict, full: dict[str, dict] | None) -> dict:
    """Overlay the full store row on a sensor row, joined by voicemail_id.

    Sensor order, heard state and computed display fields are kept; numbers,
    transcript and snippet come from the store. A caller_title the sensor
    masked is dropped so the full number is shown instead.
    """
    if not full:
        return sensor_row
    vid = str(sensor_row.get("voicemail_id") or "")
    key = vid if vid in full else None
    if key is None and vid.startswith("vm-"):
        # The redacted sensor publishes a number-derived id as "vm-<short_id>".
        sid = vid[3:].upper()
        for k, r in full.items():
            if str(r.get("short_id") or pyscript_short_id(k)).upper() == sid:
                key = k
                break
    if key is None:
        return sensor_row
    row = full[key]
    merged = dict(sensor_row)
    for field, value in row.items():
        if field == "heard":
            continue
        if value in (None, "") and merged.get(field) not in (None, ""):
            continue
        merged[field] = value
    title = merged.get("caller_title")
    if isinstance(title, str) and MASK_CHAR in title:
        merged.pop("caller_title", None)
    merged["voicemail_id"] = key  # full id, so clear/mark-heard hit the store key
    merged.pop("redacted", None)
    return merged


def pyscript_short_id(vid: str) -> str:
    """Same FNV-1a 64-bit short id the pyscript computes (_short_id)."""
    h = 14695981039346656037
    for b in str(vid).encode("utf-8"):
        h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016X}"[-10:]


def token_ok(presented: str, expected: str) -> bool:
    if not expected or not presented:
        return False
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def _box(app) -> dict:
    """Mutable runtime state.

    aiohttp freezes the Application mapping once startup begins, so later
    updates go in this dict, which is inserted before the app starts.
    """
    box = app.get(RT)
    if isinstance(box, dict):
        return box
    box = {}
    app[RT] = box
    return box


def ensure_runtime(app: web.Application) -> inbox_store.VoicemailStore:
    box = _box(app)
    existing = box.get("store")
    if existing is not None:
        return existing
    store = inbox_store.VoicemailStore(data_dir() / "inbox.json", audio_root())
    box["store"] = store
    # While pyscript still owns the live inbox, do not snapshot it or write the
    # once-only marker. The copy runs on the start where legacy mode is off.
    if legacy_enabled():
        box["migration"] = {"status": "deferred"}
    else:
        box["migration"] = legacy_import.import_legacy_once(
            store,
            legacy_paths(),
            data_dir() / "legacy_import.json",
            mirror_dirs(),
        )
    if drive_ready() and box.get("session") is not None:
        client = _build_drive_client(app)
        queue = drive_backend.DriveQueue(
            data_dir() / "drive_queue.json",
            client,
            store,
            keep_local_copy(),
        )
        box["drive_client"] = client
        box["drive_queue"] = queue
        for item in store.list_items():
            if item.get("drive_status") != "uploaded":
                queue.enqueue_upload(str(item.get("voicemail_id") or ""))
    box["sensor_dirty"] = True
    return store


def _build_drive_client(app: web.Application) -> drive_backend.DriveClient:
    raw = str(OPTS.get("google_service_account_json") or "")
    account = drive_backend.parse_service_account(raw)
    oauth = None
    if account is None:
        oauth = {
            "refresh_token": str(OPTS.get("google_oauth_refresh_token") or ""),
            "client_id": str(OPTS.get("google_oauth_client_id") or ""),
            "client_secret": str(OPTS.get("google_oauth_client_secret") or ""),
        }
        if account is None and raw:
            remember_secret(raw)

    async def transport(method, url, headers, body):
        session = _box(app).get("session")
        if session is None:
            raise RuntimeError("no-session")
        timeout = ClientTimeout(total=60)
        async with session.request(method, url, headers=headers, data=body, timeout=timeout) as resp:
            return resp.status, await resp.read()

    return drive_backend.DriveClient(
        folder_id=str(OPTS.get("google_drive_folder_id") or ""),
        service_account=account,
        oauth=oauth,
        transport=transport,
        secrets=list(_SECRETS),
        log=log,
    )


async def ha_get_state(session: ClientSession, entity_id: str) -> dict | None:
    if MOCK and not _in_ha():
        return json.loads(EXAMPLE.read_text(encoding="utf-8"))
    if _in_ha():
        url = f"http://supervisor/core/api/states/{entity_id}"
        hdr = {"Authorization": f"Bearer {_supervisor_token()}"}
        try:
            async with session.get(url, headers=hdr) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning("states %s -> HTTP %s", entity_id, resp.status)
        except Exception as exc:  # noqa: BLE001
            log.warning("states %s error: %s", entity_id, type(exc).__name__)
    store = read_store_file()
    if store:
        return store
    return None


async def ha_call_service(
    session: ClientSession, domain: str, service: str, data: dict | None = None
) -> tuple[int, Any]:
    if MOCK and not _in_ha():
        return 200, {"ok": True, "mock": True, "domain": domain, "service": service, "data": data}
    if not _in_ha():
        return 503, {"ok": False, "error": "not-in-ha"}
    url = f"http://supervisor/core/api/services/{domain}/{service}"
    hdr = {
        "Authorization": f"Bearer {_supervisor_token()}",
        "Content-Type": "application/json",
    }
    async with session.post(url, headers=hdr, json=data or {}) as resp:
        try:
            body = await resp.json(content_type=None)
        except Exception:
            body = await resp.text()
        return resp.status, body


async def ha_set_state(session: ClientSession, entity_id: str, payload: dict) -> tuple[int, Any]:
    if not _in_ha():
        return 0, {"skipped": True}
    url = f"http://supervisor/core/api/states/{entity_id}"
    hdr = {
        "Authorization": f"Bearer {_supervisor_token()}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(url, headers=hdr, json=payload) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = await resp.text()
            return resp.status, body
    except Exception as exc:  # noqa: BLE001
        log.warning("set state failed: %s", type(exc).__name__)
        return 0, {"ok": False}


async def publish_now(app: web.Application) -> bool:
    if legacy_enabled() or not _in_ha():
        return True
    if not SENSOR_RE.fullmatch(SENSOR):
        log.warning("sensor entity refused")
        return False
    session = _box(app).get("session")
    store = _box(app).get("store")
    if session is None or store is None:
        return False
    payload = inbox_store.sensor_state(store.list_items())
    status, _body = await ha_set_state(session, SENSOR, payload)
    if 200 <= status < 300:
        return True
    log.warning("sensor publish HTTP %s", status)
    return False


def _ip_allowed(peer: str) -> bool:
    if not ALLOW:
        return True
    if peer in ALLOW:
        return True
    for entry in ALLOW:
        if entry.endswith("/16") and entry.count(".") >= 1:
            prefix = ".".join(entry.split("/")[0].split(".")[:2]) + "."
            if peer.startswith(prefix):
                return True
        if entry.endswith("/24"):
            prefix = ".".join(entry.split("/")[0].split(".")[:3]) + "."
            if peer.startswith(prefix):
                return True
    return False


def deny_client(request: web.Request) -> web.Response | None:
    if not ALLOW:
        return None
    peer = request.remote or ""
    if not _ip_allowed(peer):
        return web.Response(status=403, text="forbidden")
    return None


def _ingress_user_id(request: web.Request) -> str:
    return (
        (request.headers.get("X-Remote-User-Id") or "").strip()
        or (request.headers.get("X-Hass-User-ID") or "").strip()
    )


def deny_mutation(request: web.Request) -> web.Response | None:
    """Gate clear/play/mark_heard to authenticated Supervisor ingress sessions."""
    denied = deny_client(request)
    if denied:
        return denied
    peer = request.remote or ""
    if peer != SUPERVISOR_INGRESS_IP:
        return web.json_response(
            {
                "ok": False,
                "error": "ingress-only",
                "detail": "mutations require Supervisor ingress proxy",
            },
            status=403,
        )
    if not _ingress_user_id(request):
        return web.json_response(
            {
                "ok": False,
                "error": "ingress-user-required",
                "detail": "mutations require X-Remote-User-Id from HA ingress session",
            },
            status=403,
        )
    return None


def bearer_header(request: web.Request) -> str:
    """Authorization bearer only. Body fields and other headers are not tokens."""
    auth = request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def deny_sensitive_read(request: web.Request) -> web.Response | None:
    """Inbox, audio, and diag: ingress session, or the ingest bearer.

    A sibling on 172.30.0.0/16 without one of those is refused.
    """
    denied = deny_client(request)
    if denied:
        return denied
    if token_ok(bearer_header(request), INGEST_TOKEN):
        return None
    return deny_mutation(request)


def media_source_for(item: dict | None) -> str | None:
    if not item or not item.get("audio_local") or not item.get("audio_name"):
        return None
    root = audio_root().resolve()
    media = pathlib.Path("/media").resolve()
    try:
        rel = root.relative_to(media)
    except ValueError:
        return None
    name = pathlib.Path(str(item["audio_name"])).name
    prefix = rel.as_posix()
    if prefix == ".":
        return f"media-source://media_source/local/{name}"
    return f"media-source://media_source/local/{prefix}/{name}"


def _content_type(path: pathlib.Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


async def api_health(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


DRY_RUN_ENTITY = "input_boolean.work_forward_dry_run"
PIPELINE_ENTITY = "sensor.voicemail_pipeline"


async def dry_run_status(app: web.Application) -> dict:
    """Read-only view of the house dry_run controls. Never writes anything.

    blueprint_default_input is a hard-coded constant: the blueprint's dry_run
    input default. It is not read from any live automation.
    """
    out = {"work_forward_dry_run": "unknown", "push_owner": "unknown", "blueprint_default_input": "on"}
    session = _box(app).get("session")
    if not _in_ha() or session is None:
        return out
    hdr = {"Authorization": f"Bearer {_supervisor_token()}"}
    for entity_id in (DRY_RUN_ENTITY, PIPELINE_ENTITY):
        try:
            async with session.get(
                f"http://supervisor/core/api/states/{entity_id}", headers=hdr, timeout=ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    continue
                body = await resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("dry_run status %s error: %s", entity_id, type(exc).__name__)
            continue
        if entity_id == DRY_RUN_ENTITY:
            state = str(body.get("state", "")).lower()
            out["work_forward_dry_run"] = state if state in ("on", "off") else "unknown"
        else:
            owner = (body.get("attributes") or {}).get("push_owner")
            out["push_owner"] = str(owner) if owner in ("pyscript", "blueprint") else "unknown"
    return out


async def api_diag(request: web.Request) -> web.Response:
    denied = deny_sensitive_read(request)
    if denied:
        return denied
    tok = _supervisor_token()
    box = _box(request.app)
    migration = box.get("migration") or {}
    queue = box.get("drive_queue")
    return web.json_response(
        {
            "ok": True,
            "v": VERSION,
            "in_ha": bool(tok),
            "token_present": bool(tok),
            "mock": MOCK and not bool(tok),
            "sensor": SENSOR,
            "media_player": MEDIA_PLAYER,
            "ingress_ip": SUPERVISOR_INGRESS_IP,
            "mutation_trust": "ingress-user",
            "legacy_pyscript": legacy_enabled(),
            "storage_backend": storage_backend(),
            "keep_local_copy": keep_local_copy(),
            "ingest_configured": bool(INGEST_TOKEN),
            "trust_configured": bool(VOICE_TRUST_BASE and VOICE_TRUST_TOKEN),
            "drive_configured": drive_ready(),
            "audio_root": str(audio_root()),
            "queue_depth": len(queue.jobs()) if queue is not None else 0,
            "migration": migration.get("status"),
            "import_from_share": SHARE_IMPORT.get("status") or "not_run",
            "import_marker": (data_dir() / share_import.MARKER_NAME).exists(),
            "dry_run": await dry_run_status(request.app),
        }
    )


async def api_voicemails(request: web.Request) -> web.Response:
    denied = deny_sensitive_read(request)
    if denied:
        return denied
    if legacy_enabled():
        session: ClientSession = _box(request.app)["session"]
        state = await ha_get_state(session, SENSOR)
        if not state:
            return web.json_response(
                {"ok": False, "error": "sensor-unavailable", "count": 0, "voicemails": []},
                status=502,
            )
        attrs = state.get("attributes") or {}
        raw_list = attrs.get("voicemails") or []
        full = read_store_rows()
        merged = [merge_store_row(x, full) for x in raw_list if isinstance(x, dict)]
        items = [normalize_item(x) for x in merged]
        return web.json_response(
            {
                "ok": True,
                "v": VERSION,
                "mock": MOCK and not _in_ha(),
                "legacy_pyscript": True,
                "rows_from": "store" if full is not None else "sensor",
                "count": len(items),
                "state": state.get("state"),
                "unheard": attrs.get("unheard"),
                "sensor": SENSOR,
                "media_player": MEDIA_PLAYER,
                "voicemails": items,
            }
        )
    store = ensure_runtime(request.app)
    rows = store.list_items()
    if not rows and MOCK and not _in_ha():
        state = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        attrs = state.get("attributes") or {}
        items = [normalize_item(x) for x in (attrs.get("voicemails") or []) if isinstance(x, dict)]
        return web.json_response(
            {
                "ok": True,
                "v": VERSION,
                "mock": True,
                "legacy_pyscript": False,
                "count": len(items),
                "state": state.get("state"),
                "unheard": attrs.get("unheard"),
                "sensor": SENSOR,
                "media_player": MEDIA_PLAYER,
                "voicemails": items,
            }
        )
    items = [normalize_item(row) for row in rows]
    unheard = sum(1 for row in items if not row.get("heard"))
    return web.json_response(
        {
            "ok": True,
            "v": VERSION,
            "mock": False,
            "legacy_pyscript": False,
            "count": len(items),
            "state": str(len(items)),
            "unheard": unheard,
            "sensor": SENSOR,
            "media_player": MEDIA_PLAYER,
            "voicemails": items,
        }
    )


async def _after_change(app: web.Application) -> bool:
    box = _box(app)
    box["sensor_dirty"] = True
    if box.get("session") is None:
        return False
    ok = await publish_now(app)
    if ok:
        box["sensor_dirty"] = False
    return ok


async def api_clear_one(request: web.Request) -> web.Response:
    denied = deny_mutation(request)
    if denied:
        return denied
    body = await request.json()
    vid = (body.get("voicemail_id") or body.get("message_id") or "").strip()
    if not vid:
        return web.json_response({"ok": False, "error": "voicemail_id required"}, status=400)
    if legacy_enabled():
        status, resp = await ha_call_service(
            _box(request.app)["session"],
            "pyscript",
            "inbound_voicemail_clear_one",
            {"voicemail_id": vid},
        )
        return web.json_response(
            {"ok": status < 300, "status": status, "result": resp},
            status=status if status >= 400 else 200,
        )
    store = ensure_runtime(request.app)
    removed = store.clear_one(vid)
    if removed is None:
        return web.json_response({"ok": False, "error": "not-found"}, status=404)
    queue = _box(request.app).get("drive_queue")
    if queue is not None:
        queue.enqueue_delete(removed)
    await _after_change(request.app)
    return web.json_response({"ok": True, "voicemail_id": vid})


async def api_clear_all(request: web.Request) -> web.Response:
    denied = deny_mutation(request)
    if denied:
        return denied
    if legacy_enabled():
        status, resp = await ha_call_service(
            _box(request.app)["session"], "pyscript", "inbound_voicemail_clear", {}
        )
        return web.json_response(
            {"ok": status < 300, "status": status, "result": resp},
            status=status if status >= 400 else 200,
        )
    store = ensure_runtime(request.app)
    removed = store.clear_all()
    queue = _box(request.app).get("drive_queue")
    if queue is not None:
        for item in removed:
            queue.enqueue_delete(item)
    await _after_change(request.app)
    return web.json_response({"ok": True, "cleared": len(removed)})


async def api_mark_heard(request: web.Request) -> web.Response:
    denied = deny_mutation(request)
    if denied:
        return denied
    body = await request.json()
    data = {}
    if body.get("short_id"):
        data["short_id"] = body["short_id"]
    if body.get("voicemail_id"):
        data["voicemail_id"] = body["voicemail_id"]
    if not data:
        return web.json_response({"ok": False, "error": "id required"}, status=400)
    if legacy_enabled():
        status, resp = await ha_call_service(
            _box(request.app)["session"], "pyscript", "inbound_voicemail_mark_heard", data
        )
        return web.json_response(
            {"ok": status < 300, "status": status, "result": resp},
            status=status if status >= 400 else 200,
        )
    store = ensure_runtime(request.app)
    found = store.mark_heard(
        voicemail_id=str(data.get("voicemail_id") or ""),
        short_id=str(data.get("short_id") or ""),
    )
    if not found:
        return web.json_response({"ok": False, "error": "not-found"}, status=404)
    await _after_change(request.app)
    return web.json_response({"ok": True})


async def api_play(request: web.Request) -> web.Response:
    denied = deny_mutation(request)
    if denied:
        return denied
    body = await request.json()
    call_id = inbox_store.safe_id(body.get("call_id") or "")
    if not call_id:
        return web.json_response({"ok": False, "error": "call_id required"}, status=400)
    if "media_player" in body and str(body.get("media_player") or "").strip() not in ("", MEDIA_PLAYER):
        return web.json_response(
            {
                "ok": False,
                "error": "media_player-not-overridable",
                "detail": "player is fixed in add-on options",
            },
            status=400,
        )
    if not MEDIA_PLAYER:
        return web.json_response(
            {
                "ok": False,
                "error": "media_player-not-configured",
                "detail": "set media_player to play on a speaker; the in-app player does not need one",
            },
            status=400,
        )
    if legacy_enabled():
        ext = "mp3"
        found = audio_file_for(call_id)
        if found:
            ext = found.suffix.lstrip(".")
        status, resp = await ha_call_service(
            _box(request.app)["session"],
            "script",
            "voicemail_play_audio",
            {"call_id": call_id, "media_player": MEDIA_PLAYER, "ext": ext},
        )
        return web.json_response(
            {"ok": status < 300, "status": status, "result": resp, "ext": ext},
            status=status if status >= 400 else 200,
        )
    store = ensure_runtime(request.app)
    item = store.find_by_call(call_id)
    content_id = media_source_for(item)
    if not content_id:
        return web.json_response(
            {
                "ok": False,
                "error": "speaker-needs-local-media",
                "detail": "in-app playback streams through CallCatch; a speaker needs the file under Media",
            },
            status=409,
        )
    ext = str((item or {}).get("audio_ext") or "wav")
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg"}.get(ext, "audio/wav")
    status, resp = await ha_call_service(
        _box(request.app)["session"],
        "media_player",
        "play_media",
        {
            "entity_id": MEDIA_PLAYER,
            "media_content_id": content_id,
            "media_content_type": mime,
        },
    )
    return web.json_response(
        {"ok": status < 300, "status": status, "result": resp, "ext": ext},
        status=status if status >= 400 else 200,
    )


async def api_audio(request: web.Request) -> web.Response:
    denied = deny_sensitive_read(request)
    if denied:
        return denied
    call_id = inbox_store.safe_id(request.match_info.get("call_id", ""))
    if not call_id:
        return web.Response(status=404, text="audio not found")
    if not legacy_enabled():
        store = ensure_runtime(request.app)
        item = store.find_by_call(call_id)
        if item:
            path = store.audio_file(item)
            if path is not None:
                return web.FileResponse(
                    path,
                    headers={"Content-Type": _content_type(path), "Cache-Control": "private, no-store"},
                )
            file_id = str(item.get("drive_audio_id") or "")
            client = _box(request.app).get("drive_client")
            if file_id and client is not None:
                try:
                    data = await client.download(file_id)
                except drive_backend.DriveError:
                    return web.Response(status=502, text="audio unavailable")
                ext = str(item.get("audio_ext") or "wav")
                mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg"}.get(
                    ext, "application/octet-stream"
                )
                return web.Response(
                    body=data,
                    content_type=mime,
                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
                )
    path = audio_file_for(call_id)
    if path is None:
        return web.Response(status=404, text="audio not found")
    return web.FileResponse(
        path,
        headers={"Content-Type": _content_type(path), "Cache-Control": "private, no-store"},
    )


async def _read_ingest(request: web.Request) -> tuple[dict, bytes, str]:
    ctype = (request.content_type or "").lower()
    if ctype == "application/json":
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("bad-payload")
        audio = inbox_store.decode_audio_b64(body.get("audio_base64"), AUDIO_LIMIT)
        hinted = str(body.get("audio_ext") or "")
        return body, audio, hinted
    if ctype.startswith("multipart/"):
        reader = await request.multipart()
        body: dict = {}
        audio = b""
        hinted = ""
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name in ("metadata", "json"):
                parsed = json.loads(await part.read())
                if not isinstance(parsed, dict):
                    raise ValueError("bad-payload")
                body = parsed
            elif part.name == "audio":
                audio = await part.read()
                if len(audio) > AUDIO_LIMIT:
                    raise ValueError("audio-too-large")
                hinted = pathlib.Path(part.filename or "").suffix
        return body, audio, hinted
    raise ValueError("unsupported-type")


async def api_ingest(request: web.Request) -> web.Response:
    """Bearer-authenticated inbox write for CallCatch Voice on the add-on network.

    The Authorization bearer is checked before the body is read. A JSON token
    field is not a credential. Parse failures are a generic 400.
    """
    denied = deny_client(request)
    if denied:
        return denied
    if not INGEST_TOKEN:
        return web.json_response({"ok": False, "error": "ingest-disabled"}, status=503)
    if not token_ok(bearer_header(request), INGEST_TOKEN):
        log.warning("ingest unauthorized")
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body, audio, hinted = await _read_ingest(request)
        item = inbox_store.item_from_payload(body)
    except Exception:
        log.warning("ingest rejected")
        return web.json_response({"ok": False, "error": "bad-request"}, status=400)
    if not audio:
        mirrored = legacy_import.read_mirrored_audio(item, mirror_dirs())
        if mirrored:
            audio = mirrored
            hinted = "wav"
    ext = inbox_store.guess_ext(audio, hinted) if audio else "wav"
    store = ensure_runtime(request.app)
    queue = _box(request.app).get("drive_queue")
    item["source"] = "ingest"
    item["drive_status"] = "pending" if queue is not None else "local"
    saved = store.upsert(item, audio=audio or None, ext=ext, preserve_heard=True)
    if queue is not None:
        queue.enqueue_upload(saved["voicemail_id"])
    await _after_change(request.app)
    log.info(
        "ingest id=%s audio=%s",
        saved.get("voicemail_id"),
        "yes" if saved.get("has_audio") else "no",
    )
    return web.json_response(
        {
            "ok": True,
            "voicemail_id": saved.get("voicemail_id"),
            "call_id": saved.get("call_id"),
            "has_audio": bool(saved.get("has_audio")),
        }
    )


# --------------------------------------------------------------------------
# HA event bus: the notification's "Mark heard" button fires
# `callcatch_command` {cmd: mark_heard, voicemail_id}. CallCatch listens on
# the Supervisor Core websocket proxy with its own SUPERVISOR_TOKEN. No other
# add-on calls this App, and no new port is opened.
# --------------------------------------------------------------------------

COMMAND_EVENT = "callcatch_command"
CORE_WS_URL = os.environ.get("VM_CORE_WS", "ws://supervisor/core/websocket")


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


async def handle_command(app: web.Application, data: Any) -> dict:
    """Apply one callcatch_command event. Only mark_heard is accepted."""
    if not isinstance(data, dict) or data.get("cmd") != "mark_heard":
        return {"ok": False, "error": "unknown-cmd"}
    vid = inbox_store.safe_id(data.get("voicemail_id") or "")
    if not vid:
        return {"ok": False, "error": "voicemail_id required"}
    if _truthy(data.get("dry_run")):
        log.info("command dry_run mark_heard (no change)")
        return {"ok": True, "dry_run": True}
    if legacy_enabled():
        status, _ = await ha_call_service(
            _box(app)["session"], "pyscript", "inbound_voicemail_mark_heard", {"voicemail_id": vid}
        )
        return {"ok": status < 300, "status": status}
    store = ensure_runtime(app)
    if not store.mark_heard(voicemail_id=vid):
        return {"ok": False, "error": "not-found"}
    await _after_change(app)
    return {"ok": True}


async def command_listener_once(app: web.Application, url: str, token: str) -> None:
    session: ClientSession = _box(app)["session"]
    async with session.ws_connect(url, heartbeat=30) as ws:
        first = await ws.receive_json()
        if first.get("type") != "auth_required":
            raise RuntimeError("unexpected-hello")
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError("auth-failed")
        await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": COMMAND_EVENT})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            try:
                frame = json.loads(msg.data)
            except ValueError:
                continue
            if frame.get("type") != "event":
                continue
            event = frame.get("event") or {}
            result = await handle_command(app, event.get("data"))
            log.info("command %s ok=%s", (event.get("data") or {}).get("cmd"), result.get("ok"))


async def command_listener(app: web.Application) -> None:
    delay = 5
    while True:
        try:
            await command_listener_once(app, CORE_WS_URL, _supervisor_token())
            delay = 5
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("command listener %s", type(exc).__name__)
            delay = min(delay * 2, 300)
        await asyncio.sleep(delay)


def _trust_ready() -> bool:
    return bool(VOICE_TRUST_BASE and VOICE_TRUST_TOKEN)


async def _voice_trust(app: web.Application, method: str, path: str, body: dict | None = None) -> web.Response:
    if not _trust_ready():
        return web.json_response({"ok": False, "error": "trust-not-configured"}, status=503)
    session: ClientSession | None = _box(app).get("session")
    if session is None:
        return web.json_response({"ok": False, "error": "not-ready"}, status=503)
    headers = {"Authorization": f"Bearer {VOICE_TRUST_TOKEN}"}
    try:
        async with session.request(
            method, VOICE_TRUST_BASE + path, json=body, headers=headers,
            timeout=ClientTimeout(total=10), allow_redirects=False,
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = None
            status = resp.status
    except (ClientError, asyncio.TimeoutError) as exc:
        log.warning("voice trust %s %s", path, type(exc).__name__)
        return web.json_response({"ok": False, "error": "voice-unreachable"}, status=502)
    if not isinstance(data, dict):
        return web.json_response({"ok": False, "error": "voice-bad-response"}, status=502)
    if status >= 400:
        err = str(data.get("error") or "voice-error")[:40]
        return web.json_response({"ok": False, "error": err}, status=status if status in (400, 403, 404) else 502)
    return web.json_response(data)


async def _trust_json(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return None
    return body if isinstance(body, dict) else None


async def api_trust_get(request: web.Request) -> web.Response:
    # Full numbers: the same gate as mutations (HA ingress session only).
    denied = deny_mutation(request)
    if denied:
        return denied
    return await _voice_trust(request.app, "GET", "/api/trust")


async def api_trust_add(request: web.Request) -> web.Response:
    denied = deny_mutation(request)
    if denied:
        return denied
    body = await _trust_json(request)
    if body is None or body.get("list") not in ("allow", "block") or not digits(body.get("number")):
        return web.json_response({"ok": False, "error": "bad-request"}, status=400)
    payload = {"list": body["list"], "number": str(body.get("number"))[:32], "note": str(body.get("note") or "")[:80]}
    return await _voice_trust(request.app, "POST", "/api/trust/add", payload)


async def api_trust_remove(request: web.Request) -> web.Response:
    denied = deny_mutation(request)
    if denied:
        return denied
    body = await _trust_json(request)
    if body is None or body.get("list") not in ("allow", "block") or not digits(body.get("number")):
        return web.json_response({"ok": False, "error": "bad-request"}, status=400)
    payload = {"list": body["list"], "number": str(body.get("number"))[:32]}
    return await _voice_trust(request.app, "POST", "/api/trust/remove", payload)


async def index(_request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html")


async def background(app: web.Application) -> None:
    delay = 2
    try:
        while True:
            try:
                box = _box(app)
                if box.get("sensor_dirty"):
                    ok = await publish_now(app)
                    if ok:
                        box["sensor_dirty"] = False
                        delay = 2
                    else:
                        delay = min(delay * 2, 60)
                queue = box.get("drive_queue")
                if queue is not None:
                    await queue.pump_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("background %s", type(exc).__name__)
            await asyncio.sleep(delay if _box(app).get("sensor_dirty") else 2)
    except asyncio.CancelledError:
        return


async def on_startup(app: web.Application) -> None:
    global IN_HA
    box = _box(app)
    box["session"] = ClientSession(timeout=ClientTimeout(total=30))
    IN_HA = _in_ha()
    ensure_runtime(app)
    box["bg"] = asyncio.create_task(background(app))
    if IN_HA:
        box["commands"] = asyncio.create_task(command_listener(app))
    migration = box.get("migration") or {}
    log.info(
        "CallCatch %s on :%d mock=%s in_ha=%s sensor=%s legacy=%s backend=%s audio_root=%s migration=%s",
        VERSION,
        PORT,
        MOCK and not IN_HA,
        IN_HA,
        SENSOR,
        legacy_enabled(),
        storage_backend(),
        audio_root(),
        migration.get("status"),
    )


async def on_cleanup(app: web.Application) -> None:
    box = _box(app)
    commands = box.get("commands")
    if commands is not None:
        commands.cancel()
    task = box.get("bg")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    session = box.get("session")
    if session is not None:
        await session.close()


# Browser hardening for every response (HTML, JS, JSON, audio). HA ingress
# forwards add-on response headers unchanged, so these reach the browser.
# No inline script or style is used by the UI; keep it that way.
CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "font-src 'self'",
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


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc
        for key, value in SECURITY_HEADERS.items():
            resp.headers.setdefault(key, value)
        raise
    except Exception as exc:  # noqa: BLE001 - unhandled crash: answer 500 with headers, no details
        log.error("unhandled error on %s: %s", request.path, type(exc).__name__)
        resp = web.json_response({"ok": False, "error": "internal"}, status=500)
    for key, value in SECURITY_HEADERS.items():
        resp.headers.setdefault(key, value)
    if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "private, no-store"
    return resp


def make_app() -> web.Application:
    app = web.Application(client_max_size=40 * 1024 * 1024, middlewares=[security_headers])
    app[RT] = {}
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/diag", api_diag)
    app.router.add_get("/api/voicemails", api_voicemails)
    app.router.add_post("/api/ingest", api_ingest)
    app.router.add_post("/api/clear_one", api_clear_one)
    app.router.add_post("/api/clear_all", api_clear_all)
    app.router.add_post("/api/mark_heard", api_mark_heard)
    app.router.add_post("/api/play", api_play)
    app.router.add_get("/api/audio/{call_id}", api_audio)
    app.router.add_get("/api/trust", api_trust_get)
    app.router.add_post("/api/trust/add", api_trust_add)
    app.router.add_post("/api/trust/remove", api_trust_remove)
    app.router.add_get("/", index)
    app.router.add_static("/", STATIC, show_index=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


# Store migration (plan C3): allow-listed /data files staged in
# /share/.migrate-2026-11-01/voicemail/ by the owner. Destinations are
# relative to data_dir() (/data/callcatch).
SHARE_IMPORT_ALLOW = {
    "inbox.json": "inbox.json",
    "legacy_import.json": "legacy_import.json",
    "drive_queue.json": "drive_queue.json",
}
SHARE_IMPORT: dict = {}


def run_share_import() -> dict:
    """One-shot import before the store opens. Never raises."""
    try:
        result = share_import.import_from_share("voicemail", data_dir(), SHARE_IMPORT_ALLOW)
    except Exception as exc:  # noqa: BLE001 - import must never stop the inbox
        log.error("import_from_share failed: %s", type(exc).__name__)
        result = {"status": "error"}
    SHARE_IMPORT.clear()
    SHARE_IMPORT.update(result)
    return result


def main() -> None:
    logging.getLogger("aiohttp.access").addFilter(NoHealthAccess())
    run_share_import()
    web.run_app(make_app(), host="0.0.0.0", port=PORT, print=lambda *_: None)


if __name__ == "__main__":
    main()
