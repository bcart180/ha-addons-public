"""iCloud contacts -> caller-ID directory (0.5.0, Phase 1).

Builds the flat {"+1XXXXXXXXXX": "Name"} map that the pyscript caller-ID
directory already reads from /config/private/caller_names.json (E.164 keys,
string values). Sources:

* iCloud CardDAV (contacts.icloud.com), fetched in memory on a timer.
* A one-off .vcf upload through the admin panel (never stored).

Merge rule: contact entries override the hand-kept map in
caller_names.manual.json; manual numbers that no contact has are kept. The
manual file is created once from the existing caller_names.json before the
first write, so nothing hand-entered is lost.

Privacy: nothing here logs or returns a name, number, e-mail, Apple ID or
response body. Logs and status carry counts, timestamps and fixed reason codes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("callcatch.contacts")

DIRECTORY_NAME = "caller_names.json"
MANUAL_NAME = "caller_names.manual.json"
STATUS_NAME = "contacts_status.json"
NAME_MAX = 80
MAX_DIRECTORY_BYTES = 4_000_000
UPLOAD_MAX_BYTES = 5 * 1024 * 1024
MIN_INTERVAL_MIN = 15
DEFAULT_INTERVAL_MIN = 30
ICLOUD_ROOT = "https://contacts.icloud.com/"

# Fixed reason codes. Status and logs only ever carry one of these.
REASONS = {
    "ok",
    "disabled",
    "not_configured",
    "auth",
    "http",
    "network",
    "parse",
    "no_addressbook",
    "empty",
    "write",
    "too_large",
    "bad_upload",
}


class SyncError(Exception):
    """Carries a fixed reason code only. Never a body, URL or credential."""

    def __init__(self, reason: str, status: int | None = None):
        self.reason = reason if reason in REASONS else "http"
        self.status = status if isinstance(status, int) else None
        super().__init__(self.reason)

    def __str__(self) -> str:  # never echo anything but the code
        return self.reason if self.status is None else f"{self.reason}:{self.status}"


# ---------------------------------------------------------------- normalize --

def normalize_e164(raw: Any) -> str:
    """Same keys the pyscript lookup builds (_cid_e164): US 10 digits -> +1.

    Extensions (x123, ;ext=, ext.) are dropped. Returns "" when the value is not
    a dialable number.
    """
    s = str(raw or "").strip()
    if s.lower().startswith("tel:"):
        s = s[4:]
    s = re.split(r"(?i)\s*(?:;ext=|;|,|\bext\.?|\bx(?=\s*\d)|#)", s, maxsplit=1)[0]
    s = s.strip()
    d = re.sub(r"\D", "", s)
    if s.startswith("00") and not s.startswith("+"):
        s = "+" + s[2:]
        d = d[2:]
    if len(d) == 10 and not s.startswith("+"):
        return "+1" + d
    if len(d) == 11 and d[0] == "1":
        return "+" + d
    if s.startswith("+") and 8 <= len(d) <= 15:
        return "+" + d
    return ""


_CTRL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


def clean_name(raw: Any) -> str:
    s = _CTRL.sub(" ", str(raw or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:NAME_MAX].strip()


# ------------------------------------------------------------------ vCards --

def _unfold(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _unescape(v: str) -> str:
    return (
        v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def _split_unescaped(v: str, sep: str = ";") -> list[str]:
    parts, cur, i = [], [], 0
    while i < len(v):
        ch = v[i]
        if ch == "\\" and i + 1 < len(v):
            cur.append(v[i : i + 2])
            i += 2
            continue
        if ch == sep:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [_unescape(p) for p in parts]


def _prop(line: str) -> tuple[str, dict, str] | None:
    # NAME;PARAM=..;PARAM=..:value   (group prefix "item1." allowed; quoted params may hold ':')
    in_q = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            head, value = line[:idx], line[idx + 1 :]
            break
    else:
        return None
    bits = head.split(";")
    name = bits[0].split(".")[-1].strip().upper()
    params: dict[str, str] = {}
    for b in bits[1:]:
        k, _, v = b.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return name, params, value


def parse_vcards(text: str) -> list[tuple[str, list[str]]]:
    """Return [(display_name, [e164, ...]), ...]. Cards without a name or a
    usable number are skipped. Never raises on malformed input."""
    cards: list[tuple[str, list[str]]] = []
    cur: dict | None = None
    for line in _unfold(str(text or "")):
        if not line.strip():
            continue
        p = _prop(line)
        if p is None:
            continue
        name, params, value = p
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            cur = {"fn": "", "n": "", "org": "", "nick": "", "tels": []}
            continue
        if cur is None:
            continue
        if name == "END" and value.strip().upper() == "VCARD":
            display = cur["fn"] or cur["n"] or cur["org"] or cur["nick"]
            display = clean_name(display)
            nums: list[str] = []
            for t in cur["tels"]:
                e = normalize_e164(t)
                if e and e not in nums:
                    nums.append(e)
            if display and nums:
                cards.append((display, nums))
            cur = None
            continue
        if params.get("ENCODING", "").upper() in ("QUOTED-PRINTABLE", "B", "BASE64"):
            if name != "TEL":
                continue
        if name == "FN":
            cur["fn"] = clean_name(_unescape(value))
        elif name == "N":
            parts = _split_unescaped(value)
            family = parts[0] if parts else ""
            given = parts[1] if len(parts) > 1 else ""
            cur["n"] = clean_name(f"{given} {family}")
        elif name == "ORG":
            cur["org"] = clean_name(_split_unescaped(value)[0])
        elif name == "NICKNAME":
            cur["nick"] = clean_name(_split_unescaped(value, ",")[0])
        elif name == "TEL":
            cur["tels"].append(value)
    return cards


def build_map(cards: Iterable[tuple[str, list[str]]]) -> dict[str, str]:
    """First card that claims a number keeps it (stable across syncs because
    CardDAV hrefs are sorted before parsing)."""
    out: dict[str, str] = {}
    for display, nums in cards:
        for n in nums:
            out.setdefault(n, display)
    return out


def merge(manual: dict[str, str], contacts: dict[str, str]) -> dict[str, str]:
    """Contacts override manual entries; manual-only numbers are kept."""
    out = dict(manual)
    out.update(contacts)
    return dict(sorted(out.items()))


# ------------------------------------------------------------------- files --

def _read_map(path: pathlib.Path) -> dict[str, str] | None:
    try:
        if path.stat().st_size > MAX_DIRECTORY_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k.strip()] = clean_name(v)
    return out


def atomic_write_json(path: pathlib.Path, data: Any) -> None:
    """Temp file in the same folder (0600), fsync, rename, chmod 0600."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        dfd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class Directory:
    """caller_names.json plus its preserved manual source."""

    def __init__(self, folder: pathlib.Path):
        self.folder = pathlib.Path(folder)
        self.path = self.folder / DIRECTORY_NAME
        self.manual_path = self.folder / MANUAL_NAME

    def ensure_manual(self) -> dict[str, str]:
        """Create caller_names.manual.json once from the current directory."""
        if self.manual_path.exists():
            data = _read_map(self.manual_path)
            if data is None:
                raise SyncError("write")
            return data
        current = _read_map(self.path)
        if current is None:
            # Unreadable existing directory: refuse rather than lose entries.
            raise SyncError("write")
        atomic_write_json(self.manual_path, current)
        log.info("contacts: manual directory preserved entries=%d", len(current))
        return current

    def write(self, contacts: dict[str, str]) -> dict[str, str]:
        manual = self.ensure_manual()
        merged = merge(manual, contacts)
        atomic_write_json(self.path, merged)
        return merged


# ----------------------------------------------------------------- CardDAV --

Transport = Callable[[str, str, dict, bytes | None], Awaitable[tuple[int, str]]]

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:carddav"}

PROP_PRINCIPAL = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)
PROP_HOME = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    b"<d:prop><c:addressbook-home-set/></d:prop></d:propfind>"
)
PROP_BOOKS = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
)
REPORT_CARDS = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<c:addressbook-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
    b"<d:prop><d:getetag/><c:address-data/></d:prop></c:addressbook-query>"
)


def _xml(text: str) -> ET.Element:
    try:
        return ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError as exc:
        raise SyncError("parse") from exc


def _first_href(root: ET.Element, prop_path: str) -> str:
    el = root.find(f".//{prop_path}/d:href", NS)
    return (el.text or "").strip() if el is not None and el.text else ""


class CardDAVClient:
    def __init__(self, transport: Transport, root: str = ICLOUD_ROOT, host_suffix: str = ".icloud.com"):
        self.transport = transport
        self.root = root
        self.host_suffix = host_suffix

    def _host_ok(self, url: str) -> bool:
        # Credentials only ever go to https://*.icloud.com, whatever a server's
        # href says.
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        return parts.scheme == "https" and (host == self.host_suffix.lstrip(".") or host.endswith(self.host_suffix))

    async def _call(self, method: str, url: str, depth: str, body: bytes) -> str:
        if not self._host_ok(url):
            raise SyncError("parse")
        headers = {"Depth": depth, "Content-Type": "application/xml; charset=utf-8"}
        for _hop in range(4):
            try:
                status, text = await self.transport(method, url, headers, body)
            except SyncError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - map to a code, never echo
                raise SyncError("network") from exc
            if status in (301, 302, 307, 308):
                url = urljoin(url, text or "")
                if not text or not self._host_ok(url):
                    raise SyncError("http", status)
                continue
            break
        else:
            raise SyncError("http", status)
        if status in (401, 403):
            raise SyncError("auth", status)
        if status != 207 and not (200 <= status < 300):
            raise SyncError("http", status)
        return text

    async def discover(self) -> list[str]:
        root = _xml(await self._call("PROPFIND", self.root, "0", PROP_PRINCIPAL))
        principal = _first_href(root, "d:current-user-principal")
        if not principal:
            raise SyncError("parse")
        principal_url = urljoin(self.root, principal)
        root = _xml(await self._call("PROPFIND", principal_url, "0", PROP_HOME))
        home = _first_href(root, "c:addressbook-home-set")
        if not home:
            raise SyncError("no_addressbook")
        home_url = urljoin(principal_url, home)
        root = _xml(await self._call("PROPFIND", home_url, "1", PROP_BOOKS))
        books: list[str] = []
        for resp in root.findall("d:response", NS):
            href = resp.find("d:href", NS)
            if href is None or not href.text:
                continue
            if resp.find(".//d:resourcetype/c:addressbook", NS) is None:
                continue
            url = urljoin(home_url, href.text.strip())
            if url not in books:
                books.append(url)
        if not books:
            raise SyncError("no_addressbook")
        return sorted(books)

    async def fetch_cards(self) -> list[str]:
        """All vCard texts from every address book (ALL contacts), sorted by href."""
        out: list[tuple[str, str]] = []
        for book in await self.discover():
            root = _xml(await self._call("REPORT", book, "1", REPORT_CARDS))
            for resp in root.findall("d:response", NS):
                href = resp.find("d:href", NS)
                data = resp.find(".//c:address-data", NS)
                if data is not None and data.text:
                    out.append(((href.text or "") if href is not None else "", data.text))
        out.sort(key=lambda x: x[0])
        return [t for _, t in out]


def aiohttp_transport(session: Any, apple_id: str, app_password: str) -> Transport:
    from aiohttp import BasicAuth, ClientTimeout

    auth = BasicAuth(apple_id, app_password)

    async def _t(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
        async with session.request(
            method,
            url,
            headers=headers,
            data=body,
            auth=auth,
            allow_redirects=False,
            timeout=ClientTimeout(total=60),
        ) as resp:
            if resp.status in (301, 302, 307, 308):
                return resp.status, resp.headers.get("Location", "")
            if resp.status >= 300 and resp.status != 207:
                return resp.status, ""
            raw = await resp.content.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise SyncError("too_large")
            return resp.status, raw.decode("utf-8", errors="replace")

    return _t


# ------------------------------------------------------------------ status --

class ContactsSync:
    def __init__(
        self,
        directory: Directory,
        status_path: pathlib.Path | None,
        *,
        enabled: bool,
        apple_id: str,
        app_password: str,
        interval_min: Any = DEFAULT_INTERVAL_MIN,
        transport_factory: Callable[[], Transport] | None = None,
    ):
        self.directory = directory
        self.status_path = pathlib.Path(status_path) if status_path else None
        self.enabled = bool(enabled)
        self._apple_id = str(apple_id or "").strip()
        self._app_password = str(app_password or "").strip()
        self.interval_min = clamp_interval(interval_min)
        self.transport_factory = transport_factory
        self.lock = asyncio.Lock()
        self.status: dict[str, Any] = {
            "state": "disabled",
            "source": None,
            "last_sync": None,
            "last_success": None,
            "numbers": 0,
            "contacts": 0,
            "directory_entries": 0,
            "manual_entries": 0,
        }
        self._load_status()
        self.status["enabled"] = self.enabled
        self.status["configured"] = self.configured
        self.status["interval_min"] = self.interval_min
        if not self.enabled:
            self.status["state"] = "disabled"
        elif not self.configured:
            self.status["state"] = "not_configured"

    @property
    def configured(self) -> bool:
        return bool(self._apple_id and self._app_password)

    def _load_status(self) -> None:
        if not self.status_path:
            return
        try:
            data = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            for k in ("source", "last_sync", "last_success", "numbers", "contacts", "directory_entries", "manual_entries", "state"):
                if k in data:
                    self.status[k] = data[k]

    def _save_status(self) -> None:
        if not self.status_path:
            return
        try:
            atomic_write_json(self.status_path, public_status(self.status))
        except OSError:
            log.warning("contacts: status save failed")

    def snapshot(self) -> dict[str, Any]:
        return public_status(self.status)

    def _apply(self, source: str, cards: list[tuple[str, list[str]]]) -> dict[str, Any]:
        mapping = build_map(cards)
        now = int(time.time())
        self.status["last_sync"] = now
        self.status["source"] = source
        if not mapping:
            # Never wipe the directory because a fetch or file came back empty.
            self.status["state"] = "empty"
            self._save_status()
            log.warning("contacts: %s returned no numbers; directory left unchanged", source)
            return self.snapshot()
        try:
            merged = self.directory.write(mapping)
            manual = len(self.directory.ensure_manual())
        except SyncError:
            self.status["state"] = "write"
            self._save_status()
            log.warning("contacts: directory write refused")
            return self.snapshot()
        except OSError as exc:
            self.status["state"] = "write"
            self._save_status()
            log.warning("contacts: directory write failed (%s)", type(exc).__name__)
            return self.snapshot()
        self.status.update(
            {
                "state": "ok",
                "last_success": now,
                "numbers": len(mapping),
                "contacts": len(cards),
                "directory_entries": len(merged),
                "manual_entries": manual,
            }
        )
        self._save_status()
        log.info(
            "contacts: synced %d numbers from %d contacts via %s; directory entries=%d",
            len(mapping),
            len(cards),
            source,
            len(merged),
        )
        return self.snapshot()

    async def sync_icloud(self) -> dict[str, Any]:
        if not self.enabled:
            self.status["state"] = "disabled"
            return self.snapshot()
        if not self.configured or self.transport_factory is None:
            self.status["state"] = "not_configured"
            return self.snapshot()
        async with self.lock:
            try:
                client = CardDAVClient(self.transport_factory())
                texts = await client.fetch_cards()
                cards: list[tuple[str, list[str]]] = []
                for t in texts:
                    cards.extend(parse_vcards(t))
            except SyncError as exc:
                self.status["state"] = exc.reason
                self.status["last_sync"] = int(time.time())
                self.status["http_status"] = exc.status
                self._save_status()
                log.warning("contacts: iCloud sync failed (%s)", exc)
                return self.snapshot()
            self.status.pop("http_status", None)
            return self._apply("icloud", cards)

    async def import_vcf(self, raw: bytes) -> dict[str, Any]:
        if len(raw) > UPLOAD_MAX_BYTES:
            return {**self.snapshot(), "state": "too_large"}
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        if "BEGIN:VCARD" not in text.upper():
            return {**self.snapshot(), "state": "bad_upload"}
        async with self.lock:
            cards = parse_vcards(text)
            return self._apply("upload", cards)

    async def run_forever(self) -> None:
        if not self.enabled or not self.configured:
            log.info("contacts: iCloud sync %s", "not configured" if self.enabled else "disabled")
            return
        await asyncio.sleep(5)
        while True:
            try:
                await self.sync_icloud()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the loop alive, log type only
                self.status["state"] = "http"
                log.warning("contacts: sync crashed (%s)", type(exc).__name__)
            await asyncio.sleep(self.interval_min * 60)


PUBLIC_KEYS = (
    "state",
    "enabled",
    "configured",
    "interval_min",
    "source",
    "last_sync",
    "last_success",
    "numbers",
    "contacts",
    "directory_entries",
    "manual_entries",
    "http_status",
)


def public_status(status: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in PUBLIC_KEYS:
        if k not in status:
            continue
        v = status[k]
        if k == "state":
            v = v if v in REASONS else "http"
        elif k == "source":
            v = v if v in ("icloud", "upload") else None
        elif k in ("enabled", "configured"):
            v = bool(v)
        elif v is not None and not isinstance(v, int):
            v = None
        out[k] = v
    return out


def clamp_interval(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_INTERVAL_MIN
    return max(MIN_INTERVAL_MIN, min(n, 1440))
