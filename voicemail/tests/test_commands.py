"""0.3.1 callcatch_command listener (notification Mark heard). Fake 555 numbers only."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load

import inbox_store  # noqa: E402  (path set by test_trust)


def _seed(mod, app):
    store = mod.ensure_runtime(app)
    item = inbox_store.item_from_payload({"call_id": "CA5550200", "from": "+15555550120"})
    saved = store.upsert(item)
    return store, saved["voicemail_id"]


def _heard(store, vid):
    return any(i["voicemail_id"] == vid and i.get("heard") for i in store.list_items())


@pytest.mark.asyncio
async def test_handle_command_mark_heard_and_guards(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        store, vid = _seed(mod, app)
        assert (await mod.handle_command(app, {"cmd": "delete", "voicemail_id": vid}))["error"] == "unknown-cmd"
        assert (await mod.handle_command(app, "mark_heard"))["error"] == "unknown-cmd"
        assert (await mod.handle_command(app, {"cmd": "mark_heard"}))["ok"] is False
        dry = await mod.handle_command(app, {"cmd": "mark_heard", "voicemail_id": vid, "dry_run": "true"})
        assert dry["dry_run"] is True and not _heard(store, vid)
        assert (await mod.handle_command(app, {"cmd": "mark_heard", "voicemail_id": vid}))["ok"] is True
        assert _heard(store, vid)
        missing = await mod.handle_command(app, {"cmd": "mark_heard", "voicemail_id": "nope555"})
        assert missing["error"] == "not-found"


@pytest.mark.asyncio
async def test_handle_command_request_callback_queues_and_does_not_dial(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    fired = []
    services = []

    async def fake_fire(_session, event_type, data):
        fired.append((event_type, data))
        return 200, {"ok": True, "mock": True}

    async def fake_service(*_args, **_kwargs):
        services.append(True)
        return 200, {}

    mod.ha_fire_event = fake_fire
    mod.ha_call_service = fake_service
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        store, vid = _seed(mod, app)
        assert (await mod.handle_command(app, {"cmd": "place_phone_call", "voicemail_id": vid}))["error"] == "unknown-cmd"
        dry = await mod.handle_command(app, {"cmd": "request_callback", "voicemail_id": vid, "dry_run": True})
        assert dry["dry_run"] is True
        assert store.get(vid)["callback_status"] == ""
        first = await mod.handle_command(app, {"cmd": "request_callback", "voicemail_id": vid})
        assert first["ok"] is True and first["event_fired"] is True and first["idempotent"] is False
        assert store.get(vid)["callback_status"] == "pending"
        assert store.get(vid)["followup_state"] == "queued"
        second = await mod.handle_command(app, {"cmd": "request_callback", "voicemail_id": vid})
        assert second["idempotent"] is True and second["event_fired"] is False
        missing = await mod.handle_command(app, {"cmd": "request_callback", "voicemail_id": "nope555"})
        assert missing["error"] == "not-found"
    assert services == []
    assert len(fired) == 1
    assert fired[0][0] == "callcatch_callback_requested"
    assert set(fired[0][1]) == {"voicemail_id", "from_masked", "display_name", "summary", "requested_at"}
    assert fired[0][1]["from_masked"] == "•••0120"
    assert "5555550120" not in repr(fired[0][1]) and "+15555550120" not in repr(fired[0][1])


@pytest.mark.asyncio
async def test_listener_auths_subscribes_and_applies_event(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    frames = []
    seeded = {}
    done = asyncio.Event()

    async def fake_core(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        frames.append(await ws.receive_json())
        await ws.send_json({"type": "auth_ok"})
        frames.append(await ws.receive_json())
        await ws.send_json({"id": 1, "type": "result", "success": True})
        await ws.send_json({"id": 1, "type": "event", "event": {
            "event_type": "callcatch_command",
            "data": {"cmd": "mark_heard", "voicemail_id": seeded["vid"]},
        }})
        await done.wait()
        await ws.close()
        return ws

    core = web.Application()
    core.router.add_get("/websocket", fake_core)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        store, vid = _seed(mod, app)
        seeded["vid"] = vid
        async with TestServer(core) as cs:
            task = asyncio.create_task(
                mod.command_listener_once(app, str(cs.make_url("/websocket")), "sup-token-test")
            )
            for _ in range(100):
                if _heard(store, vid):
                    break
                await asyncio.sleep(0.02)
            done.set()
            await asyncio.wait_for(task, 5)
    assert frames[0] == {"type": "auth", "access_token": "sup-token-test"}
    assert frames[1]["type"] == "subscribe_events" and frames[1]["event_type"] == "callcatch_command"
    assert _heard(store, vid)
