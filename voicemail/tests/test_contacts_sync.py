"""0.5.0 contacts sync: vCard parsing, E.164, merge, atomic write, redaction,
upload limits and auth, mocked CardDAV. Fake 555-01xx numbers and fake names only."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import re
import stat
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import contacts_sync as cs  # noqa: E402

FAKE_NAMES = ("Testy McTestface", "Avery Example", "Sample Plumbing Co", "Jordan Placeholder")

VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:McTestface;Testy;;;\r\nFN:Testy McTestface\r\n"
    "item1.TEL;type=CELL;type=pref:(555) 555-0101\r\n"
    "TEL;TYPE=HOME:+1 555-555-0102\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Example;Avery;;;\r\n"
    "TEL;VALUE=uri:tel:+1-555-555-0103;ext=12\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nORG:Sample Plumbing Co;\r\nTEL:555.555.0104 x9\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Jordan Place\r\n holder\r\nTEL:5555550105\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:No Number Person\r\nEMAIL:nobody@example.invalid\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nTEL:5555550106\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bad Tel\r\nTEL:12\r\nEND:VCARD\r\n"
)


# ------------------------------------------------------------- normalize --

@pytest.mark.parametrize(
    "raw,want",
    [
        ("(555) 555-0101", "+15555550101"),
        ("555.555.0101", "+15555550101"),
        ("1-555-555-0101", "+15555550101"),
        ("+1 (555) 555-0101", "+15555550101"),
        ("tel:+1-555-555-0101", "+15555550101"),
        ("555-555-0101 x123", "+15555550101"),
        ("555-555-0101;ext=4", "+15555550101"),
        ("+44 20 7946 0000", "+442079460000"),
        ("0044 20 7946 0000", "+442079460000"),
        ("12", ""),
        ("", ""),
        (None, ""),
        ("not a number", ""),
    ],
)
def test_normalize(raw, want):
    assert cs.normalize_e164(raw) == want


def test_normalize_matches_pyscript_keys():
    # pyscript _cid_e164: 10 digits -> +1, 11 starting 1 -> +, '+' 8..15 digits kept.
    for raw in ("5555550101", "15555550101", "+15555550101"):
        assert cs.normalize_e164(raw) == "+15555550101"


# ---------------------------------------------------------------- parse --

def test_parse_vcards_edge_cases():
    cards = cs.parse_vcards(VCF)
    m = cs.build_map(cards)
    assert m == {
        "+15555550101": "Testy McTestface",
        "+15555550102": "Testy McTestface",
        "+15555550103": "Avery Example",  # from N when FN missing; uri + ext
        "+15555550104": "Sample Plumbing Co",  # ORG fallback
        "+15555550105": "Jordan Placeholder",  # folded line
    }
    assert len(cards) == 4


def test_parse_lf_only_escapes_and_garbage():
    text = "junk\nBEGIN:VCARD\nFN:Avery\\, Example\nTEL:555 555 0107\nEND:VCARD\nBEGIN:VCARD\nFN:Unclosed\n"
    assert cs.build_map(cs.parse_vcards(text)) == {"+15555550107": "Avery, Example"}
    assert cs.parse_vcards("") == []
    assert cs.parse_vcards("\x00\x01:::") == []


def test_name_cleaned_and_capped():
    text = "BEGIN:VCARD\nFN:" + "A\u202e" + "b" * 200 + "\nTEL:5555550108\nEND:VCARD\n"
    name = cs.build_map(cs.parse_vcards(text))["+15555550108"]
    assert len(name) <= cs.NAME_MAX and "\u202e" not in name


def test_first_card_keeps_shared_number():
    text = "BEGIN:VCARD\nFN:Avery Example\nTEL:5555550109\nEND:VCARD\nBEGIN:VCARD\nFN:Testy McTestface\nTEL:5555550109\nEND:VCARD\n"
    assert cs.build_map(cs.parse_vcards(text)) == {"+15555550109": "Avery Example"}


# ---------------------------------------------------------------- merge --

def test_merge_precedence():
    manual = {"+15555550101": "House Line", "+15555550110": "Manual Only"}
    contacts = {"+15555550101": "Testy McTestface", "+15555550102": "Avery Example"}
    out = cs.merge(manual, contacts)
    assert out == {
        "+15555550101": "Testy McTestface",
        "+15555550102": "Avery Example",
        "+15555550110": "Manual Only",
    }


def test_directory_preserves_manual_once(tmp_path):
    d = cs.Directory(tmp_path)
    (tmp_path / cs.DIRECTORY_NAME).write_text(json.dumps({"+15555550110": "Manual Only", "+15555550101": "Old"}))
    d.write({"+15555550101": "Testy McTestface"})
    manual = json.loads((tmp_path / cs.MANUAL_NAME).read_text())
    assert manual == {"+15555550110": "Manual Only", "+15555550101": "Old"}
    # Second sync: contact removed from iCloud -> manual value comes back, manual file unchanged.
    out = d.write({"+15555550102": "Avery Example"})
    assert out == {"+15555550101": "Old", "+15555550102": "Avery Example", "+15555550110": "Manual Only"}
    assert json.loads((tmp_path / cs.MANUAL_NAME).read_text()) == manual
    # Consumer format: flat str->str with E.164 keys.
    data = json.loads((tmp_path / cs.DIRECTORY_NAME).read_text())
    assert all(re.fullmatch(r"\+\d{8,15}", k) and isinstance(v, str) for k, v in data.items())


def test_manual_seeded_from_readonly_legacy(tmp_path):
    legacy = tmp_path / "config_private" / cs.DIRECTORY_NAME
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"+15555550110": "Manual Only", "+15555550101": "Old"}))
    os.chmod(legacy, 0o400)
    d = cs.Directory(tmp_path / "share" / "callcatch" / "private", legacy)
    out = d.write({"+15555550101": "Testy McTestface"})
    assert out == {"+15555550101": "Testy McTestface", "+15555550110": "Manual Only"}
    assert json.loads(d.manual_path.read_text()) == {"+15555550110": "Manual Only", "+15555550101": "Old"}
    assert json.loads(legacy.read_text()) == {"+15555550110": "Manual Only", "+15555550101": "Old"}
    assert stat.S_IMODE(d.folder.stat().st_mode) == 0o700


def test_server_paths_stay_off_config(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.delenv("VM_CALLER_DIR", raising=False)
    mod = _load_server(monkeypatch, tmp)
    monkeypatch.delenv("VM_CALLER_DIR", raising=False)
    assert str(mod.caller_dir()) == "/share/callcatch/private"
    assert str(mod.legacy_caller_file()) == "/homeassistant/private/caller_names.json"


def test_unreadable_directory_refused(tmp_path):
    (tmp_path / cs.DIRECTORY_NAME).write_text("{not json")
    with pytest.raises(cs.SyncError):
        cs.Directory(tmp_path).write({"+15555550101": "Testy McTestface"})
    assert (tmp_path / cs.DIRECTORY_NAME).read_text() == "{not json"


def test_atomic_write_mode_600(tmp_path):
    p = tmp_path / "private" / cs.DIRECTORY_NAME
    old = os.umask(0o022)
    try:
        cs.atomic_write_json(p, {"+15555550101": "Testy McTestface"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text()) == {"+15555550101": "Testy McTestface"}
    assert [x.name for x in p.parent.iterdir()] == [cs.DIRECTORY_NAME]


def test_atomic_write_failure_leaves_original(tmp_path, monkeypatch):
    p = tmp_path / cs.DIRECTORY_NAME
    cs.atomic_write_json(p, {"+15555550101": "Old"})

    def boom(*_a, **_k):
        raise OSError("disk")

    monkeypatch.setattr(cs.os, "replace", boom)
    with pytest.raises(OSError):
        cs.atomic_write_json(p, {"+15555550101": "New"})
    assert json.loads(p.read_text()) == {"+15555550101": "Old"}
    assert [x.name for x in tmp_path.iterdir()] == [cs.DIRECTORY_NAME]


# --------------------------------------------------------------- CardDAV --

ROOT_URL = "https://contacts.icloud.com/"
PRINCIPAL = '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/100000/principal/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>'
HOME = '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:response><d:href>/100000/principal/</d:href><d:propstat><d:prop><c:addressbook-home-set><d:href>https://p00-contacts.icloud.com:443/100000/carddavhome/</d:href></c:addressbook-home-set></d:prop></d:propstat></d:response></d:multistatus>'
BOOKS = '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:response><d:href>/100000/carddavhome/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response><d:response><d:href>/100000/carddavhome/card/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:addressbook/></d:resourcetype></d:prop></d:propstat></d:response></d:multistatus>'


def _report(vcf):
    from xml.sax.saxutils import escape

    return (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav"><d:response><d:href>/100000/carddavhome/card/a.vcf</d:href>'
        f"<d:propstat><d:prop><c:address-data>{escape(vcf)}</c:address-data></d:prop></d:propstat></d:response></d:multistatus>"
    )


def mock_transport(report_vcf=VCF, fail=None):
    calls = []

    async def t(method, url, headers, body):
        calls.append((method, url, headers.get("Depth")))
        if fail:
            return fail
        if method == "PROPFIND" and url == ROOT_URL:
            return 207, PRINCIPAL
        if method == "PROPFIND" and url.endswith("/principal/"):
            return 207, HOME
        if method == "PROPFIND" and url.endswith("/carddavhome/"):
            return 207, BOOKS
        if method == "REPORT":
            return 207, _report(report_vcf)
        return 404, ""

    t.calls = calls
    return t


def test_carddav_discovery_and_fetch():
    t = mock_transport()
    texts = asyncio.run(cs.CardDAVClient(t).fetch_cards())
    assert len(texts) == 1 and "BEGIN:VCARD" in texts[0]
    assert [c[0] for c in t.calls] == ["PROPFIND", "PROPFIND", "PROPFIND", "REPORT"]
    assert t.calls[-1][1] == "https://p00-contacts.icloud.com:443/100000/carddavhome/card/"
    assert t.calls[-1][2] == "1"


def test_carddav_refuses_non_icloud_host():
    evil = HOME.replace("https://p00-contacts.icloud.com:443", "https://attacker.example")
    calls = []

    async def t(method, url, headers, body):
        calls.append(url)
        if url == ROOT_URL:
            return 207, PRINCIPAL
        if url.endswith("/principal/"):
            return 207, evil
        return 207, BOOKS

    with pytest.raises(cs.SyncError):
        asyncio.run(cs.CardDAVClient(t).fetch_cards())
    assert not any("attacker" in u for u in calls)


def _sync(tmp_path, transport, enabled=True, pw="fake-app-pw-0000"):
    return cs.ContactsSync(
        cs.Directory(tmp_path / "private"),
        tmp_path / "status.json",
        enabled=enabled,
        apple_id="someone@example.invalid",
        app_password=pw,
        interval_min=30,
        transport_factory=lambda: transport,
    )


def test_sync_end_to_end(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / cs.DIRECTORY_NAME).write_text(json.dumps({"+15555550110": "Manual Only"}))
    s = _sync(tmp_path, mock_transport())
    snap = asyncio.run(s.sync_icloud())
    assert snap["state"] == "ok" and snap["numbers"] == 5 and snap["contacts"] == 4
    assert snap["directory_entries"] == 6 and snap["manual_entries"] == 1
    data = json.loads((tmp_path / "private" / cs.DIRECTORY_NAME).read_text())
    assert data["+15555550110"] == "Manual Only"
    status = (tmp_path / "status.json").read_text()
    assert not re.search(r"\d{4}", status.replace(str(snap["last_sync"]), "").replace(str(snap["last_success"]), ""))
    for n in FAKE_NAMES:
        assert n not in status


def test_sync_auth_error_no_echo(tmp_path, caplog):
    s = _sync(tmp_path, mock_transport(fail=(401, "secret body someone@example.invalid")))
    with caplog.at_level(logging.DEBUG):
        snap = asyncio.run(s.sync_icloud())
    assert snap["state"] == "auth"
    blob = caplog.text + json.dumps(snap)
    assert "someone@example.invalid" not in blob and "fake-app-pw" not in blob and "secret body" not in blob


def test_sync_network_error_no_echo(tmp_path, caplog):
    async def t(*_a):
        raise RuntimeError("boom https://someone:fake-app-pw-0000@contacts.icloud.com/")

    s = _sync(tmp_path, t)
    with caplog.at_level(logging.DEBUG):
        snap = asyncio.run(s.sync_icloud())
    assert snap["state"] == "network"
    assert "fake-app-pw" not in caplog.text and "someone" not in caplog.text


def test_empty_result_does_not_wipe(tmp_path):
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / cs.DIRECTORY_NAME).write_text(json.dumps({"+15555550110": "Manual Only"}))
    s = _sync(tmp_path, mock_transport(report_vcf="BEGIN:VCARD\nFN:X\nEND:VCARD\n"))
    snap = asyncio.run(s.sync_icloud())
    assert snap["state"] == "empty"
    assert not (tmp_path / "private" / cs.MANUAL_NAME).exists()


def test_disabled_and_unconfigured(tmp_path):
    assert asyncio.run(_sync(tmp_path, mock_transport(), enabled=False).sync_icloud())["state"] == "disabled"
    assert asyncio.run(_sync(tmp_path, mock_transport(), pw="").sync_icloud())["state"] == "not_configured"
    assert not (tmp_path / "private").exists()


def test_interval_clamped():
    assert cs.clamp_interval(5) == 15
    assert cs.clamp_interval("x") == 30
    assert cs.clamp_interval(60) == 60


def test_log_redaction(tmp_path, caplog):
    s = _sync(tmp_path, mock_transport())
    with caplog.at_level(logging.DEBUG, logger="callcatch.contacts"):
        asyncio.run(s.sync_icloud())
        asyncio.run(s.import_vcf(VCF.encode()))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "synced 5 numbers from 4 contacts" in text
    assert not re.search(r"555|\d{4,}", text)
    for n in FAKE_NAMES + ("someone@example.invalid", "fake-app-pw"):
        assert n not in text


# ----------------------------------------------------- upload endpoint --

def _load_server(monkeypatch, tmp):
    monkeypatch.setenv("VM_OPTIONS", "/nonexistent/options.json")
    monkeypatch.setenv("VM_MOCK", "1")
    monkeypatch.setenv("VM_DATA_DIR", tmp)
    monkeypatch.setenv("VM_CALLER_DIR", str(pathlib.Path(tmp) / "private"))
    monkeypatch.setenv("VM_AUDIO_ROOT", str(pathlib.Path(tmp) / "audio"))
    monkeypatch.setenv("VM_LEGACY_PATHS", str(pathlib.Path(tmp) / "missing.json"))
    for name in list(sys.modules):
        if name == "server":
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.ALLOW = ["172.30.0.0/16"]
    mod.SUPERVISOR_INGRESS_IP = "172.30.32.2"
    return mod


class _Content:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    async def read(self, n):
        out = self.data[self.pos : self.pos + n]
        self.pos += len(out)
        return out


class _App(dict):
    pass


class _Req:
    def __init__(self, app, remote, headers=None, body=b"", content_length=None):
        self.app = app
        self.remote = remote
        self.headers = headers or {}
        self.content = _Content(body)
        self.content_length = len(body) if content_length is None else content_length


def _app(mod):
    from aiohttp import web

    app = web.Application()
    app[mod.RT] = {}
    return app


INGRESS = {"X-Remote-User-Id": "fake-admin-id"}


@pytest.mark.asyncio
async def test_upload_requires_ingress_user(monkeypatch):
    tmp = tempfile.mkdtemp()
    mod = _load_server(monkeypatch, tmp)
    app = _app(mod)
    r = await mod.api_contacts_upload(_Req(app, "172.30.33.9", INGRESS, VCF.encode()))
    assert r.status == 403
    r = await mod.api_contacts_upload(_Req(app, "172.30.32.2", {}, VCF.encode()))
    assert r.status == 403
    r = await mod.api_contacts_status(_Req(app, "172.30.33.9", {}))
    assert r.status == 403
    assert not (pathlib.Path(tmp) / "private").exists()


@pytest.mark.asyncio
async def test_upload_size_limit(monkeypatch):
    tmp = tempfile.mkdtemp()
    mod = _load_server(monkeypatch, tmp)
    app = _app(mod)
    big = b"BEGIN:VCARD\n" + b"x" * (cs.UPLOAD_MAX_BYTES + 10)
    r = await mod.api_contacts_upload(_Req(app, "172.30.32.2", INGRESS, big))
    assert r.status == 413
    # Lying Content-Length: streamed bytes still capped.
    r = await mod.api_contacts_upload(_Req(app, "172.30.32.2", INGRESS, big, content_length=10))
    assert r.status == 413
    assert not (pathlib.Path(tmp) / "private").exists()


@pytest.mark.asyncio
async def test_upload_merges_and_keeps_no_file(monkeypatch):
    tmp = tempfile.mkdtemp()
    mod = _load_server(monkeypatch, tmp)
    app = _app(mod)
    before = {p for p in pathlib.Path(tmp).rglob("*")}
    r = await mod.api_contacts_upload(_Req(app, "172.30.32.2", INGRESS, VCF.encode()))
    assert r.status == 200
    body = json.loads(r.body)
    assert body["state"] == "ok" and body["numbers"] == 5 and body["source"] == "upload"
    for n in FAKE_NAMES:
        assert n not in r.body.decode()
    assert not re.search(r"555", r.body.decode())
    created = {p.name for p in pathlib.Path(tmp).rglob("*")} - {p.name for p in before}
    assert created <= {"private", cs.DIRECTORY_NAME, cs.MANUAL_NAME, cs.STATUS_NAME, "callcatch"}
    assert not any(p.suffix == ".vcf" for p in pathlib.Path(tmp).rglob("*"))
    d = pathlib.Path(tmp) / "private" / cs.DIRECTORY_NAME
    assert stat.S_IMODE(d.stat().st_mode) == 0o600
    r = await mod.api_contacts_upload(_Req(app, "172.30.32.2", INGRESS, b"hello"))
    assert r.status == 400
