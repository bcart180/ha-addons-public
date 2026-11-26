"""Opt-in send-only domain setup (0.8.9). Mocked AgentMail HTTP, fake data only."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib

from aiohttp.test_utils import TestClient, TestServer

from test_dedicated_sender import KEY, PINNED
from test_trust import _load

import notify_send  # noqa: E402
import sender_domain  # noqa: E402

DOMAIN = "callcatch.brysoncarter.com"
INBOX = "callcatch@" + DOMAIN
API = "https://api.agentmail.to"
USER = {"X-Remote-User-Id": "user-555"}
PHONE = "555-555-0199"
LEAK = "leak@example.com"


class _Content:
    def __init__(self, data: bytes):
        self.data = data

    async def read(self, n=-1):
        take = len(self.data) if n is None or n < 0 else n
        out, self.data = self.data[:take], self.data[take:]
        return out


class _Resp:
    def __init__(self, status, payload):
        raw = b"" if payload is None else json.dumps(payload).encode()
        self.status = status
        self.content = _Content(raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


def _records(value: str) -> list[dict]:
    return [
        {
            "type": "TXT",
            "name": DOMAIN,
            "value": value,
            "status": "MISSING",
            "reason": "dns_records_missing " + PHONE + " " + LEAK,
        },
        {
            "type": "MX",
            "name": "mail." + DOMAIN,
            "value": "feedback-smtp.agentmail.to",
            "priority": 10,
            "status": "MISSING",
        },
        {"type": "SECRET", "name": "hidden", "value": KEY, "status": "MISSING"},
    ]


class _AgentMail:
    """Tiny AgentMail stand-in. Speaks only the routes the setup step may call."""

    def __init__(self, *, spf: str = "v=spf1 include:spf.agentmail.to -all"):
        self.spf = spf
        self.domain = None
        self.inboxes = {}
        self.calls = []
        self.promote = False
        self.conflict = False
        self.paginate = False
        self.flags = (False, False)
        self.verify_status = 200
        self.verify_body = {"ok": True}

    def _full(self, status: str) -> dict:
        inbound, subdomains = self.flags
        return {
            "domain_id": "dom_cc_1",
            "domain": DOMAIN,
            "status": status,
            "inbound_enabled": inbound,
            "subdomains_enabled": subdomains,
            "feedback_enabled": False,
            "records": _records(self.spf),
            "pod_id": "pod-should-not-leak",
            "client_id": KEY,
        }

    def request(self, method, url, json=None, headers=None, params=None, timeout=None, allow_redirects=None, **kw):
        method = method.upper()
        assert str(url).startswith(API + "/"), url
        assert "cloudflare" not in str(url).lower()
        assert allow_redirects is False
        assert (headers or {}).get("Authorization") == "Bearer " + KEY
        path = str(url).split("?", 1)[0][len(API):]
        self.calls.append((method, path, json, dict(params or {})))
        if method == "GET" and path == "/v0/domains":
            return self._list_domains(dict(params or {}))
        if method == "POST" and path == "/v0/domains":
            return self._create_domain(json)
        if method == "GET" and path == "/v0/domains/dom_cc_1":
            return _Resp(200, self.domain)
        if method == "POST" and path == "/v0/domains/dom_cc_1/verify":
            if self.promote and self.domain:
                self.domain["status"] = "VERIFIED"
            return _Resp(self.verify_status, self.verify_body)
        if method == "GET" and path == "/v0/inboxes":
            rows = [{"email": LEAK, "inbox_id": "inb_leak", "pod_id": "pod-should-not-leak"}]
            rows += [{"email": email, "inbox_id": "inb_cc_1"} for email in self.inboxes]
            return _Resp(200, {"count": len(rows), "inboxes": rows})
        if method == "POST" and path == "/v0/inboxes":
            return self._create_inbox(json)
        return _Resp(404, {"message": KEY, "phone": PHONE})

    def _list_domains(self, params: dict):
        token = params.get("page_token") or ""
        if self.paginate and not token:
            return _Resp(200, {
                "count": 1,
                "domains": [{
                    "domain_id": "dom_other",
                    "domain": "other.example",
                    "inbound_enabled": False,
                    "subdomains_enabled": False,
                }],
                "next_page_token": "page-2",
            })
        if self.domain is None:
            return _Resp(200, {"count": 0, "domains": []})
        item = {
            "domain_id": self.domain["domain_id"],
            "domain": self.domain["domain"],
            "inbound_enabled": self.domain["inbound_enabled"],
            "subdomains_enabled": self.domain["subdomains_enabled"],
        }
        return _Resp(200, {"count": 1, "domains": [item]})

    def _create_domain(self, body):
        assert body == {
            "domain": DOMAIN,
            "inbound_enabled": False,
            "subdomains_enabled": False,
        }
        if self.conflict and self.domain is None:
            self.domain = self._full("PENDING")
            return _Resp(409, {"message": KEY, "phone": PHONE, "email": LEAK})
        if self.domain is not None:
            return _Resp(409, {"message": "exists"})
        self.domain = self._full("NOT_STARTED")
        return _Resp(200, self.domain)

    def _create_inbox(self, body):
        assert body == {"username": "callcatch", "domain": DOMAIN}
        if INBOX in self.inboxes:
            return _Resp(409, {"message": KEY, "email": LEAK})
        self.inboxes[INBOX] = True
        return _Resp(200, {"email": INBOX, "inbox_id": "inb_cc_1", "pod_id": "pod-should-not-leak", "client_id": KEY})


def _posts(api: _AgentMail, path: str) -> list:
    return [call for call in api.calls if call[0] == "POST" and call[1] == path]


def _assert_public(snap: dict):
    assert set(snap) == {"enabled", "state", "domain_status", "records", "inbox", "code"}
    blob = json.dumps(snap)
    assert KEY not in blob
    assert PHONE not in blob
    assert LEAK not in blob
    assert INBOX not in blob
    assert "pod-should-not-leak" not in blob
    assert "dom_cc_1" not in blob
    assert "@" not in blob
    for rec in snap["records"]:
        assert set(rec) == {"type", "name", "value", "priority", "status"}


async def test_blank_key_makes_no_request():
    def boom(*_a, **_k):
        raise AssertionError("AgentMail was called")

    snap = await sender_domain.ensure_sender_domain(type("S", (), {"request": boom})(), "")
    assert snap["enabled"] is True and snap["state"] == "not-configured" and snap["code"] == "no-key"
    _assert_public(snap)


async def test_domain_step_is_idempotent_and_creates_the_inbox_once(caplog):
    caplog.set_level(logging.DEBUG)
    api = _AgentMail()
    session = type("S", (), {"request": api.request})()

    first = await sender_domain.ensure_sender_domain(session, KEY)
    second = await sender_domain.ensure_sender_domain(session, KEY)
    assert first["state"] == "pending" and first["inbox"] == "skipped" and first["code"] == "not-verified"
    assert second["state"] == "pending" and second["domain_status"] == "NOT_STARTED"
    assert len(_posts(api, "/v0/domains")) == 1
    assert _posts(api, "/v0/domains")[0][2] == {
        "domain": DOMAIN,
        "inbound_enabled": False,
        "subdomains_enabled": False,
    }
    assert _posts(api, "/v0/domains/dom_cc_1/verify")
    assert _posts(api, "/v0/inboxes") == []
    assert all(path.startswith("/v0/") and "cloudflare" not in path for _m, path, _b, _p in api.calls)
    txt = next(rec for rec in first["records"] if rec["type"] == "TXT")
    mx = next(rec for rec in first["records"] if rec["type"] == "MX")
    assert txt["value"] == "v=spf1 include:spf.agentmail.to -all"
    assert txt["name"] == DOMAIN and txt["status"] == "MISSING" and txt["priority"] is None
    assert mx["priority"] == 10 and mx["value"] == "feedback-smtp.agentmail.to"
    assert all(rec["type"] != "SECRET" for rec in first["records"])
    _assert_public(first)
    _assert_public(second)

    api.promote = True
    created = await sender_domain.ensure_sender_domain(session, KEY)
    again = await sender_domain.ensure_sender_domain(session, KEY)
    assert created["state"] == "ready" and created["domain_status"] == "VERIFIED" and created["inbox"] == "created"
    assert again["state"] == "ready" and again["inbox"] == "present"
    assert len(_posts(api, "/v0/domains")) == 1
    assert len(_posts(api, "/v0/inboxes")) == 1
    assert _posts(api, "/v0/inboxes")[0][2] == {"username": "callcatch", "domain": DOMAIN}
    _assert_public(created)
    _assert_public(again)
    assert KEY not in caplog.text and PHONE not in caplog.text and LEAK not in caplog.text and INBOX not in caplog.text
    assert "type=TXT" in caplog.text and "type=MX" in caplog.text
    assert sender_domain.INBOX == PINNED
    assert notify_send.sender_allowed(sender_domain.INBOX)


async def test_existing_domain_on_the_next_page_is_not_created_again():
    api = _AgentMail()
    api.paginate = True
    api.domain = api._full("PENDING")
    session = type("S", (), {"request": api.request})()
    snap = await sender_domain.ensure_sender_domain(session, KEY)
    assert snap["state"] == "pending"
    assert _posts(api, "/v0/domains") == []
    assert ("GET", "/v0/domains", None, {"limit": "50", "page_token": "page-2"}) in [
        (m, p, b, q) for m, p, b, q in api.calls
    ]
    _assert_public(snap)


async def test_create_conflict_is_reused():
    api = _AgentMail()
    api.conflict = True
    session = type("S", (), {"request": api.request})()
    snap = await sender_domain.ensure_sender_domain(session, KEY)
    assert snap["state"] == "pending" and snap["code"] == "not-verified"
    assert len(_posts(api, "/v0/domains")) == 1
    _assert_public(snap)


async def test_wrong_flags_do_not_verify_or_create_an_inbox():
    api = _AgentMail()
    api.flags = (True, False)
    api.domain = api._full("VERIFIED")
    session = type("S", (), {"request": api.request})()
    snap = await sender_domain.ensure_sender_domain(session, KEY)
    assert snap["state"] == "error" and snap["code"] == "domain-flags"
    assert snap["records"] and snap["inbox"] == "skipped"
    assert _posts(api, "/v0/domains") == []
    assert _posts(api, "/v0/domains/dom_cc_1/verify") == []
    assert _posts(api, "/v0/inboxes") == []
    _assert_public(snap)


async def test_redirect_and_verify_error_hide_bodies(caplog):
    caplog.set_level(logging.DEBUG)

    class _Redirect:
        def __init__(self):
            self.n = 0

        def request(self, method, url, **_kw):
            self.n += 1
            assert "cloudflare" not in str(url).lower()
            return _Resp(302, {"location": "https://evil.example/hook", "message": KEY, "phone": PHONE, "email": LEAK})

    redirected = _Redirect()
    snap = await sender_domain.ensure_sender_domain(type("S", (), {"request": redirected.request})(), KEY)
    assert redirected.n == 1
    assert snap["state"] == "error" and snap["code"] == "http-302"
    assert snap["records"] == []
    _assert_public(snap)
    assert "evil.example" not in json.dumps(snap)

    api = _AgentMail()
    api.verify_status = 500
    api.verify_body = {"message": KEY + " " + PHONE + " " + LEAK}
    failed = await sender_domain.ensure_sender_domain(type("S", (), {"request": api.request})(), KEY)
    assert failed["state"] == "error" and failed["code"] == "verify-http-500"
    _assert_public(failed)
    assert KEY not in caplog.text and PHONE not in caplog.text and LEAK not in caplog.text


async def test_secret_inside_a_dns_value_is_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    api = _AgentMail(spf="v=spf1 " + KEY + " include:spf.agentmail.to -all")
    snap = await sender_domain.ensure_sender_domain(type("S", (), {"request": api.request})(), KEY)
    txt = next(rec for rec in snap["records"] if rec["type"] == "TXT")
    assert txt["value"] == "v=spf1 *** include:spf.agentmail.to -all"
    _assert_public(snap)
    assert KEY not in caplog.text


def test_setup_option_defaults_off_and_is_documented():
    root_cfg = pathlib.Path(__file__).resolve().parents[1]
    cfg = (root_cfg / "config.yaml").read_text(encoding="utf-8")
    en = (root_cfg / "translations" / "en.yaml").read_text(encoding="utf-8")
    docs = (root_cfg / "DOCS.md").read_text(encoding="utf-8")
    assert "\n  agentmail_domain_setup: false\n" in cfg
    assert "\n  agentmail_domain_setup: bool\n" in cfg
    assert "\n  agentmail_domain_setup:\n" in en
    assert DOMAIN in en and INBOX in en
    assert "does not change DNS" in docs or "does not change DNS" in en
    assert "agentmail_domain_setup" in docs
    assert "Cloudflare" in docs


async def test_diag_stays_off_until_the_option_is_on(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    async def dirty(session, key):
        seen.append(key)
        return {"api_key": key}

    m = _load(monkeypatch, tmp_path, agent_token="agent-token-555-" + "x" * 24, agentmail_api_key=KEY)
    monkeypatch.setattr(m.sender_domain, "ensure_sender_domain", dirty)
    m.SUPERVISOR_INGRESS_IP = "127.0.0.1"
    async with TestClient(TestServer(m.make_app())) as client:
        assert "sender_domain_task" not in client.server.app[m.RT]
        health = await client.get("/api/health")
        assert await health.text() == "ok\n"
        resp = await client.get("/api/diag", headers=USER)
        body = await resp.json()
    assert body["sender_domain"]["enabled"] is False
    assert body["sender_domain"]["state"] == "off"
    assert body["sender_domain"]["records"] == []
    assert seen == []
    assert KEY not in json.dumps(body) and KEY not in caplog.text
    paths = [route.resource.canonical for route in m.make_app().router.routes() if route.resource]
    assert not any("domain" in path or "agentmail" in path for path in paths)


async def test_opt_in_diag_redacts_secrets(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []

    async def dirty(session, key):
        seen.append(key)
        return {
            "enabled": True,
            "state": "ready",
            "domain_status": "VERIFIED",
            "records": [{
                "type": "TXT",
                "name": "mail." + DOMAIN,
                "value": "v=spf1 " + key,
                "priority": None,
                "status": "VALID",
                "reason": PHONE + " " + key,
            }],
            "inbox": "present",
            "code": "ok",
            "api_key": key,
            "email": LEAK,
        }

    m = _load(
        monkeypatch, tmp_path,
        agent_token="agent-token-555-" + "x" * 24,
        agentmail_api_key=KEY,
        agentmail_domain_setup=True,
    )
    monkeypatch.setattr(m.sender_domain, "ensure_sender_domain", dirty)
    m.SUPERVISOR_INGRESS_IP = "127.0.0.1"
    async with TestClient(TestServer(m.make_app())) as client:
        task = client.server.app[m.RT]["sender_domain_task"]
        await asyncio.wait_for(task, timeout=2)
        resp = await client.get("/api/diag", headers=USER)
        body = await resp.json()
        assert await (await client.get("/api/health")).text() == "ok\n"
    snap = body["sender_domain"]
    assert snap["state"] == "ready" and snap["enabled"] is True
    assert snap["records"][0]["value"] == "v=spf1 ***"
    assert set(snap["records"][0]) == {"type", "name", "value", "priority", "status"}
    blob = json.dumps(body)
    assert KEY not in blob and KEY not in caplog.text
    assert PHONE not in blob and PHONE not in caplog.text
    assert LEAK not in blob and LEAK not in caplog.text
    assert seen == [KEY]
