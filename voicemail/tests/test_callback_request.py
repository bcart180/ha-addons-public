"""Callback request: pending record + masked HA event. Never dials. Fake 555 numbers only."""
from __future__ import annotations

import logging

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load

import inbox_store  # noqa: E402  (path set by test_trust)

CALLER = "+15555550100"
OTHER = "+15555550199"
USER = {"X-Remote-User-Id": "user-555"}
FULL_NUMBERS = (CALLER, "15555550100", "5555550100", OTHER, "15555550199", "5555550199", "555-555-0199")


def _seed(mod, app):
    store = mod.ensure_runtime(app)
    item = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-screened",
            "call_id": "CA5550100",
            "from": CALLER,
            "caller_title": "Sam Rivera",
            "transcript_text": "This is Sam. Please call 555-555-0199 about the gate.",
            "summary": {
                "name": "Sam",
                "reason": "please call 555-555-0199 about the gate",
                "callback": "15555550199",
                "urgency": "normal",
            },
        }
    )
    saved = store.upsert(item)
    return store, saved


def _assert_masked(blob: str) -> None:
    for number in FULL_NUMBERS:
        assert number not in blob, number


class _Resp:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = body if body is not None else {"ok": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return ""


class _Sess:
    def __init__(self):
        self.urls: list[str] = []
        self.payloads: list[dict] = []

    def post(self, url, **kwargs):
        self.urls.append(url)
        self.payloads.append(kwargs.get("json") or {})
        return _Resp()


@pytest.fixture
def mod(monkeypatch, tmp_path):
    loaded = _load(monkeypatch, tmp_path)
    loaded.SUPERVISOR_INGRESS_IP = "127.0.0.1"
    return loaded


def test_ingest_cannot_set_callback_state():
    item = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-inject",
            "from": CALLER,
            "callback_status": "returned",
            "callback_requested_at": "1999-01-01T00:00:00+00:00",
            "callback_note": CALLER,
        }
    )
    assert item["callback_status"] == ""
    assert item["callback_requested_at"] == ""
    assert item["callback_note"] == ""


def test_event_payload_masks_from_and_summary():
    item = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-screened",
            "from": CALLER,
            "caller_title": CALLER,
            "transcript_text": "Please call 555-555-0199 about the gate.",
            "summary": {
                "name": "",
                "reason": "please call 555-555-0199 about the gate",
                "urgency": "normal",
            },
        }
    )
    import server

    payload = server.callback_event_data(item, "2026-11-05T15:00:00+00:00")
    assert set(payload) == {"voicemail_id", "from_masked", "display_name", "summary", "requested_at"}
    assert payload["from_masked"] == "•••0100"
    assert payload["display_name"] == ""
    assert "•••0199" in payload["summary"]
    _assert_masked(repr(payload))

    named = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-screened",
            "from": CALLER,
            "caller_title": "Sam Rivera",
            "summary": {"name": "Sam", "reason": "gate latch", "urgency": "normal"},
            "transcript_text": "This is Sam.",
        }
    )
    named_payload = server.callback_event_data(named, "2026-11-05T15:00:00+00:00")
    assert named_payload["display_name"] == "Sam"
    assert named_payload["from_masked"] == "•••0100"


def test_ui_call_back_requests_instead_of_dialing():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    js = (root / "rootfs" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (root / "rootfs" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    server = (root / "rootfs" / "app" / "server.py").read_text(encoding="utf-8")
    assert "api/request_callback" in js
    assert "api/callback_status" in js
    assert "Returned · ${when}" in js
    assert "That voicemail is not in the inbox." in js
    assert 'href="${esc(v.tel)}"' not in js
    assert "callTop.href" not in js
    assert "num.href" not in js
    assert '<button type="button" class="cb-btn' in js
    assert '<button type="button" class="icon-btn call-green" id="btn-call-top"' in html
    assert 'id="btn-callback">Call back</button>' in html
    assert "Mark returned" in html and "Cancel request" in html
    assert "Open on phone" in html
    for blob in (js, html, server):
        assert "place_phone_call" not in blob
        assert "<Dial" not in blob
        assert "sip:" not in blob.lower()


@pytest.mark.asyncio
async def test_request_pending_event_idempotent_and_masked_log(mod, caplog):
    caplog.set_level(logging.INFO, logger="voicemail")
    fired = []
    services = []

    async def fake_fire(_session, event_type, data):
        fired.append((event_type, data))
        return 200, {"ok": True, "mock": True}

    async def fake_service(*_args, **_kwargs):
        services.append(True)
        return 200, {"ok": True}

    mod.ha_fire_event = fake_fire
    mod.ha_call_service = fake_service
    async with TestClient(TestServer(mod.make_app())) as client:
        _app = client.server.app
        store, saved = _seed(mod, _app)
        vid = saved["voicemail_id"]
        assert (await client.post("/api/request_callback", json={"voicemail_id": vid})).status == 403
        denied = await client.post(
            "/api/request_callback",
            json={"voicemail_id": vid},
            headers={"Sec-Fetch-Site": "cross-site", **USER},
        )
        assert denied.status == 403

        first = await client.post("/api/request_callback", json={"voicemail_id": vid, "dry_run": True}, headers=USER)
        body = await first.json()
        assert first.status == 200
        assert body["ok"] is True and body["callback_status"] == "pending"
        assert body["callback_requested_at"]
        assert body["idempotent"] is False and body["event_fired"] is True
        assert body["event"] == "callcatch_callback_requested"
        row = store.get(vid)
        assert row["callback_status"] == "pending"
        assert row["callback_requested_at"] == body["callback_requested_at"]

        second = await client.post("/api/request_callback", json={"voicemail_id": vid}, headers=USER)
        again = await second.json()
        assert again["ok"] is True and again["idempotent"] is True and again["event_fired"] is False
        assert again["callback_requested_at"] == body["callback_requested_at"]
        assert len(fired) == 1
        event_type, payload = fired[0]
        assert event_type == "callcatch_callback_requested"
        assert payload["voicemail_id"] == vid
        assert payload["from_masked"] == "•••0100"
        assert payload["display_name"] == "Sam"
        assert "•••0199" in payload["summary"]
        assert set(payload) == {"voicemail_id", "from_masked", "display_name", "summary", "requested_at"}
        _assert_masked(repr(payload))
        assert services == []

        listed = await client.get("/api/voicemails", headers=USER)
        listed_row = next(x for x in (await listed.json())["voicemails"] if x["voicemail_id"] == vid)
        assert listed_row["callback_status"] == "pending"
        sensor = inbox_store.sensor_state(store.list_items())["attributes"]["voicemails"][0]
        assert sensor["callback_status"] == "pending"
        assert "callback_note" not in sensor
        assert "caller" not in sensor
        _assert_masked(repr(sensor))

        missing = await client.post("/api/request_callback", json={"voicemail_id": "missing-vm"}, headers=USER)
        assert missing.status == 404
        assert (await missing.json())["error"] == "not-found"
        assert (await client.post("/api/callback_status", json={"voicemail_id": "missing-vm", "status": "returned"}, headers=USER)).status == 404

        # Re-ingest must not clear the pending request.
        refreshed = inbox_store.item_from_payload(
            {"voicemail_id": vid, "from": CALLER, "transcript_text": "follow up", "callback_status": "returned"}
        )
        kept = store.upsert(refreshed)
        assert kept["callback_status"] == "pending"
        assert kept["callback_requested_at"] == body["callback_requested_at"]

    text = caplog.text
    _assert_masked(text)
    assert "•••0100" in text
    assert "sup-token" not in text


@pytest.mark.asyncio
async def test_status_transitions_and_unknown_status(mod, caplog):
    caplog.set_level(logging.INFO, logger="voicemail")
    services = []

    async def fake_fire(*_args, **_kwargs):
        return 200, {"ok": True, "mock": True}

    async def fake_service(*_args, **_kwargs):
        services.append(True)
        return 200, {}

    mod.ha_fire_event = fake_fire
    mod.ha_call_service = fake_service
    async with TestClient(TestServer(mod.make_app())) as client:
        store, saved = _seed(mod, client.server.app)
        vid = saved["voicemail_id"]
        early = await client.post(
            "/api/callback_status",
            json={"voicemail_id": vid, "status": "returned"},
            headers=USER,
        )
        assert early.status == 409
        assert (await early.json())["error"] == "not-pending"
        bad = await client.post(
            "/api/callback_status",
            json={"voicemail_id": vid, "status": "pending"},
            headers=USER,
        )
        assert bad.status == 400 and (await bad.json())["error"] == "bad-status"
        assert (await client.post("/api/callback_status", data="nope", headers=USER)).status == 400

        await client.post("/api/request_callback", json={"voicemail_id": vid}, headers=USER)
        note = f"left word for {OTHER}"
        returned = await client.post(
            "/api/callback_status",
            json={"voicemail_id": vid, "status": "returned", "note": note},
            headers=USER,
        )
        body = await returned.json()
        assert returned.status == 200
        assert body["callback_status"] == "returned" and body["callback_status_at"]
        assert "left word for" in body["callback_note"]
        stored = store.get(vid)
        assert stored["callback_status"] == "returned"
        sensor = inbox_store.sensor_state(store.list_items())["attributes"]["voicemails"][0]
        assert "callback_note" not in sensor
        assert OTHER not in repr(sensor)
        assert CALLER not in repr(sensor)

        repeat = await client.post(
            "/api/callback_status",
            json={"voicemail_id": vid, "status": "returned", "note": "ignore"},
            headers=USER,
        )
        repeated = await repeat.json()
        assert repeated["idempotent"] is True
        assert repeated["callback_status_at"] == body["callback_status_at"]
        assert store.get(vid)["callback_note"] != "ignore"

        closed = await client.post(
            "/api/callback_status",
            json={"voicemail_id": vid, "status": "failed"},
            headers=USER,
        )
        assert closed.status == 409

        # A new request after a terminal state is a new pending row.
        nxt = await client.post("/api/request_callback", json={"voicemail_id": vid}, headers=USER)
        nxt_body = await nxt.json()
        assert nxt_body["callback_status"] == "pending" and nxt_body["idempotent"] is False
        assert nxt_body["callback_requested_at"] != body["callback_requested_at"]

        failed = await client.post(
            "/api/callback_status", json={"voicemail_id": vid, "status": "failed"}, headers=USER
        )
        assert (await failed.json())["callback_status"] == "failed"
        await client.post("/api/request_callback", json={"voicemail_id": vid}, headers=USER)
        cancelled = await client.post(
            "/api/callback_status", json={"voicemail_id": vid, "status": "cancelled"}, headers=USER
        )
        assert (await cancelled.json())["callback_status"] == "cancelled"
        assert services == []
        _assert_masked(caplog.text)
        assert "•••0100" in caplog.text


@pytest.mark.asyncio
async def test_fire_event_mock_does_not_post_and_live_posts_events_only(mod, monkeypatch):
    posts = []
    real_post = ClientSession.post

    def wrapped(self, url, *args, **kwargs):
        posts.append(url)
        return real_post(self, url, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "post", wrapped)
    async with TestClient(TestServer(mod.make_app())) as client:
        _store, saved = _seed(mod, client.server.app)
        resp = await client.post(
            "/api/request_callback", json={"voicemail_id": saved["voicemail_id"]}, headers=USER
        )
        body = await resp.json()
        assert body["event_fired"] is True and body["callback_status"] == "pending"
    assert posts == []

    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    sess = _Sess()
    status, _body = await mod.ha_fire_event(
        sess,
        "callcatch_callback_requested",
        {
            "voicemail_id": "vm-screened",
            "from_masked": "•••0100",
            "display_name": "Sam",
            "summary": "gate",
            "requested_at": "2026-11-05T15:00:00+00:00",
        },
    )
    assert status == 200
    assert sess.urls == ["http://supervisor/core/api/events/callcatch_callback_requested"]
    assert sess.payloads[0]["from_masked"] == "•••0100"
    refused, err = await mod.ha_fire_event(sess, "place_phone_call", {})
    assert refused == 400 and err["error"] == "bad-event"
    assert len(sess.urls) == 1

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    quiet = _Sess()
    mock_status, mock_body = await mod.ha_fire_event(quiet, "callcatch_callback_requested", {"voicemail_id": "vm"})
    assert mock_status == 200 and mock_body["mock"] is True
    assert quiet.urls == []


@pytest.mark.asyncio
async def test_legacy_store_refuses_callback(mod, monkeypatch):
    monkeypatch.setattr(mod, "legacy_enabled", lambda: True)
    async with TestClient(TestServer(mod.make_app())) as client:
        resp = await client.post("/api/request_callback", json={"voicemail_id": "vm-screened"}, headers=USER)
        assert resp.status == 409
        assert (await resp.json())["error"] == "legacy-unsupported"
