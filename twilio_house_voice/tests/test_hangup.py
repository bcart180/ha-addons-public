"""Early-hangup delivery tests: every termination path yields exactly one
transcript per CallSid, delivered through a durable outbox.

Run: python3 twilio_house_voice/tests/test_hangup.py  (or pytest)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import time

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(APP))
os.environ.pop("SUPERVISOR_TOKEN", None)

import server  # noqa: E402

SILENCE = base64.b64encode(bytes([0xFF]) * 160).decode()
FROM = "+15555550100"
AUTH = "twilio-auth-token-value"
BASE = "https://voice.example"


def _sig(path, data):
    """X-Twilio-Signature as Twilio computes it (0.2.5+: routes need a token)."""
    payload = BASE + path + "".join(k + str(data[k]) for k in sorted(data))
    digest = hmac.new(AUTH.encode(), payload.encode(), hashlib.sha1).digest()
    return {"X-Twilio-Signature": base64.b64encode(digest).decode()}


def _start(token, call_id, sid="MZ00000000000000a1"):
    return {"event": "start", "start": {
        "streamSid": sid, "callSid": call_id, "tracks": ["inbound"],
        "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
        "customParameters": {"token": token},
    }}


def _stream_token(xml):
    marker = 'name="token" value="'
    i = xml.index(marker) + len(marker)
    return xml[i:xml.index('"', i)]


async def wait_for(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


class Hooks:
    """Fake HA webhook. `fail` responses are returned first (404 = HA restarting)."""

    def __init__(self, fail=0, status=404):
        self.bodies = []
        self.fail = fail
        self.status = status
        self.calls = 0

    async def handle(self, request):
        self.calls += 1
        body = await request.json()
        if self.fail > 0:
            self.fail -= 1
            return web.Response(status=self.status)
        self.bodies.append(body)
        return web.json_response({"ok": True})

    def for_call(self, call_id):
        return [b for b in self.bodies if b.get("call_id") == call_id]


async def _fake(hooks, xai_handler=None):
    fake_app = web.Application()
    fake_app.router.add_post("/hook", hooks.handle)
    if xai_handler is not None:
        fake_app.router.add_get("/v1/realtime", xai_handler)
    fake = TestServer(fake_app)
    await fake.start_server()
    return fake


def _opts(fake, **extra):
    base = {
        "xai_bridge": "stub",
        "public_base_url": BASE,
        "twilio_auth_token": AUTH,
        "ha_webhook_transcript": str(fake.make_url("/hook")),
        "record_caller_audio": True,
        "greeting_mode": "agent",
        "log_level": "error",
    }
    base.update(extra)
    return base


async def _voice(client, call_id):
    data = {"CallSid": call_id, "From": FROM, "To": "+15555550199", "Direction": "inbound"}
    got = await client.post("/twilio/voice", data=data, headers=_sig("/twilio/voice", data))
    assert got.status == 200
    return _stream_token(await got.text())


async def _status(client, call_id, status="completed"):
    data = {"CallSid": call_id, "CallStatus": status, "From": FROM}
    got = await client.post("/twilio/status", data=data, headers=_sig("/twilio/status", data))
    assert got.status == 200


def _run(coro):
    asyncio.run(coro)


# --------------------------------------------------------------------------
# Simulated Twilio sequences: hangup at different points, status before/after
# --------------------------------------------------------------------------

SCENARIOS = [
    # (name, steps) — steps run in order against one CallSid
    ("hangup_before_stream_start", ["status"]),
    ("ws_closes_before_start_event", ["ws_open", "ws_close", "status"]),
    ("hangup_right_after_start", ["ws_open", "start", "stop", "status"]),
    ("hangup_mid_media", ["ws_open", "start", "media", "media", "stop", "status"]),
    ("status_before_stop", ["ws_open", "start", "media", "status", "stop"]),
    ("status_before_abrupt_close", ["ws_open", "start", "media", "status", "ws_close"]),
    ("abrupt_close_no_stop", ["ws_open", "start", "media", "ws_close", "status"]),
    ("duplicate_status_callbacks", ["ws_open", "start", "stop", "status", "status", "status"]),
]


def test_twilio_sequences_one_transcript_per_callsid():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        try:
            for index, (name, steps) in enumerate(SCENARIOS):
                tmp = tempfile.mkdtemp()
                call_id = f"CA{index:02d}hangup0000000000"
                app = server.create_app(_opts(fake), data_dir=tmp)
                async with TestClient(TestServer(app)) as client:
                    token = await _voice(client, call_id)
                    ws = None
                    for step in steps:
                        if step == "ws_open":
                            ws = await client.ws_connect("/twilio/stream")
                        elif step == "start":
                            await ws.send_json(_start(token, call_id))
                        elif step == "media":
                            await ws.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
                        elif step == "stop":
                            await ws.send_json({"event": "stop"})
                        elif step == "ws_close":
                            await ws.close()
                        elif step == "status":
                            await asyncio.sleep(0.05)
                            await _status(client, call_id)
                    if ws is not None and not ws.closed:
                        await ws.close()
                    # The watchdog is the backstop for anything still open.
                    await wait_for(lambda: hooks.for_call(call_id), 2)
                    if not hooks.for_call(call_id):
                        await server.watchdog_tick(app, now=time.monotonic() + 3600)
                    assert await wait_for(lambda: hooks.for_call(call_id), 3), name
                    await asyncio.sleep(0.2)
                    got = hooks.for_call(call_id)
                    assert len(got) == 1, (name, len(got))
                    body = got[0]
                    assert body["early_hangup"] is True, name
                    assert body["hangup"]["by"] == "caller", name
                    assert body["state"] == "final", name
                    assert not app["sessions"], name
        finally:
            await fake.close()

    _run(go())


def test_status_while_socket_open_then_silence_watchdog_finalizes():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        call_id = "CAwatchdog0000000001"
        try:
            app = server.create_app(_opts(fake), data_dir=tmp)
            async with TestClient(TestServer(app)) as client:
                token = await _voice(client, call_id)
                ws = await client.ws_connect("/twilio/stream")
                await ws.send_json(_start(token, call_id))
                assert await wait_for(lambda: call_id in app["sessions"])
                await _status(client, call_id)
                await asyncio.sleep(0.1)
                assert hooks.for_call(call_id) == []  # the open socket owns hangup
                done = await server.watchdog_tick(app, now=time.monotonic() + server.WATCHDOG_STATUS_GRACE_S + 1)
                assert done == [call_id]
                assert len(hooks.for_call(call_id)) == 1
                assert hooks.for_call(call_id)[0]["reason"] == "watchdog:status_terminal"
                await ws.close()
                await asyncio.sleep(0.1)
                assert len(hooks.for_call(call_id)) == 1
        finally:
            await fake.close()

    _run(go())


# --------------------------------------------------------------------------
# xAI teardown: the caller's last words must survive an early hangup
# --------------------------------------------------------------------------

def _xai(behaviour):
    """behaviour: 'late_transcript' answers commit with a completed transcript;
    'greeting' starts a greeting and never finishes it; 'silent' never answers."""

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            kind = data.get("type")
            if kind == "session.update":
                await ws.send_json({"type": "session.updated"})
            elif kind == "response.create":
                await ws.send_json({"type": "response.created"})
                await ws.send_json({"type": "response.output_audio_transcript.delta", "delta": "Hi, you have reached the"})
                if behaviour != "greeting":
                    await ws.send_json({"type": "response.output_audio_transcript.done", "transcript": "Hi, you have reached the house."})
                    await ws.send_json({"type": "response.done"})
            elif kind == "input_audio_buffer.append" and behaviour in ("late_transcript", "silent"):
                await ws.send_json({"type": "input_audio_buffer.speech_started"})
                await ws.send_json({"type": "conversation.item.input_audio_transcription.updated", "transcript": "please call me"})
            elif kind == "input_audio_buffer.commit" and behaviour == "late_transcript":
                await asyncio.sleep(0.3)
                await ws.send_json({"type": "conversation.item.input_audio_transcription.completed",
                                    "transcript": "please call me back about the delivery"})
        return ws

    return handler


async def _live_call(behaviour, call_id, send_media=True):
    hooks = Hooks()
    fake = await _fake(hooks, _xai(behaviour))
    tmp = tempfile.mkdtemp()
    try:
        app = server.create_app(_opts(
            fake, xai_bridge="live", xai_api_key="xai-test-key",
            xai_realtime_url=f"ws://{fake.host}:{fake.port}/v1/realtime",
        ), data_dir=tmp)
        async with TestClient(TestServer(app)) as client:
            token = await _voice(client, call_id)
            ws = await client.ws_connect("/twilio/stream")
            await ws.send_json(_start(token, call_id))
            session = None
            assert await wait_for(lambda: app["sessions"].get(call_id) and app["sessions"][call_id].xai_ready)
            session = app["sessions"][call_id]
            await asyncio.sleep(0.1)
            if send_media:
                await ws.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
                assert await wait_for(lambda: session.awaiting_caller)
            started = time.monotonic()
            await ws.send_json({"event": "stop"})  # caller hangs up
            assert await wait_for(lambda: hooks.for_call(call_id), server.FINALIZE_DRAIN_S + 3)
            elapsed = time.monotonic() - started
            await ws.close()
            await _status(client, call_id)  # late status must not duplicate
            await asyncio.sleep(0.1)
            got = hooks.for_call(call_id)
            assert len(got) == 1
            return got[0], elapsed
    finally:
        await fake.close()


def test_caller_last_words_drained_after_hangup():
    async def go():
        body, elapsed = await _live_call("late_transcript", "CAdrain00000000001")
        assert "please call me back about the delivery" in body["transcript_text"], body["transcript_text"]
        assert body["hangup"]["phase"] in ("caller_speaking", "after_reply")
        assert elapsed < server.FINALIZE_DRAIN_S + 1

    _run(go())


def test_drain_is_bounded_when_xai_never_answers():
    async def go():
        body, elapsed = await _live_call("silent", "CAdrain00000000002")
        # The partial words are kept, and the post is not held forever.
        assert "please call me" in body["transcript_text"]
        assert body["early_hangup"] is True
        assert body["hangup"]["phase"] == "caller_speaking"
        assert server.HANGUP_NOTES["caller_speaking"] in body["transcript_text"]
        assert elapsed < server.FINALIZE_DRAIN_S + 2

    _run(go())


def test_hangup_during_greeting_is_marked():
    async def go():
        body, _ = await _live_call("greeting", "CAgreet00000000001", send_media=False)
        assert body["hangup"]["phase"] == "greeting"
        assert body["early_hangup"] is True
        assert body["transcript_text"].endswith(server.HANGUP_NOTES["greeting"])
        assert "Hi, you have reached the" in body["transcript_text"]

    _run(go())


def test_normal_end_is_not_marked_early():
    session = server.CallSession(server.create_app({"xai_bridge": "stub", "log_level": "error"}, data_dir=tempfile.mkdtemp()),
                                 "CAnormal0000000001", {"from": FROM})
    session.add_turn("assistant", "Hi.", final=True)
    session.responses_done = 1
    session.add_turn("caller", "Call me back.", final=True)
    session.add_turn("assistant", "Will do, goodbye.", final=True)
    session.responses_done = 2
    session.hangup = session.classify_hangup("stop")
    assert session.hangup["early"] is False
    assert "[Caller" not in session.transcript_text()


# --------------------------------------------------------------------------
# Process exits mid-call, ingest failures, persist failures, dedup
# --------------------------------------------------------------------------

def test_restart_mid_call_recovers_journaled_transcript_once():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        call_id = "CAcrash00000000001"
        try:
            app1 = server.create_app(_opts(fake), data_dir=tmp)
            session = server.CallSession(app1, call_id, {"from": FROM, "to": "+15555550199"})
            app1["sessions"][call_id] = session
            session.checkpoint()
            session.add_turn("assistant", "Hi, you have reached the house.", final=True)
            session.add_turn("caller", "This is Sam, call me back.", final=True)
            session.checkpoint()
            # Process dies here: no finalize, no webhook.
            assert hooks.bodies == []
            app2 = server.create_app(_opts(fake), data_dir=tmp)
            async with TestClient(TestServer(app2)) as client:  # on_startup recovers
                assert await wait_for(lambda: hooks.for_call(call_id))
                body = hooks.for_call(call_id)[0]
                assert "This is Sam, call me back." in body["transcript_text"]
                assert body["recovered"] is True and body["early_hangup"] is True
                assert server.HANGUP_NOTES["recovered"] in body["transcript_text"]
                await _status(client, call_id)  # Twilio's late status after restart
                await asyncio.sleep(0.1)
                assert len(hooks.for_call(call_id)) == 1
                await server.retry_due(app2, now=time.time() + 3600)
                assert len(hooks.for_call(call_id)) == 1
        finally:
            await fake.close()

    _run(go())


def test_status_for_orphan_journal_recovers_instead_of_empty_missed_call():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        call_id = "CAorphan0000000001"
        try:
            app = server.create_app(_opts(fake), data_dir=tmp)
            await server.on_startup(app)
            for name in ("retry_task", "watchdog_task"):
                app[name].cancel()
            session = server.CallSession(app, call_id, {"from": FROM})
            session.add_turn("caller", "Package at the door.", final=True)
            session.checkpoint()  # journal written, session lost (no entry in app["sessions"])
            await server.record_missed(app, call_id, {"From": FROM}, "status:completed")
            got = hooks.for_call(call_id)
            assert len(got) == 1 and "Package at the door." in got[0]["transcript_text"]
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_ingest_failure_is_retried_with_backoff_and_survives_restart():
    async def go():
        hooks = Hooks(fail=3, status=404)  # HA restarting: webhook not registered yet
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        call_id = "CAoutbox0000000001"
        try:
            app = server.create_app(_opts(fake), data_dir=tmp)
            await server.on_startup(app)
            for name in ("retry_task", "watchdog_task"):
                app[name].cancel()
            session = server.CallSession(app, call_id, {"from": FROM})
            app["sessions"][call_id] = session
            session.add_turn("caller", "Hello, it's the plumber.", final=True)
            await session.finalize("stop")
            side = server.load_sidecar(app, call_id)
            assert side["ha_post"]["transcript"]["ok"] is False
            first_due = side["next_retry_at"]
            # Not due yet: no attempt.
            await server.retry_due(app, now=time.time())
            assert hooks.calls == 1
            await server.retry_due(app, now=first_due + 1)
            side2 = server.load_sidecar(app, call_id)
            assert hooks.calls == 2 and side2["next_retry_at"] - (first_due + 1) > 0
            await app["client"].close()
            # Restart: a new process picks the outbox up from disk.
            app2 = server.create_app(_opts(fake), data_dir=tmp)
            await server.on_startup(app2)
            for name in ("retry_task", "watchdog_task"):
                app2[name].cancel()
            far = time.time() + 2 * 3600
            await server.retry_due(app2, now=far)
            await server.retry_due(app2, now=far + 3600)
            got = hooks.for_call(call_id)
            assert len(got) == 1 and "plumber" in got[0]["transcript_text"]
            assert server.load_sidecar(app2, call_id)["ha_post"]["transcript"]["ok"] is True
            await server.retry_due(app2, now=far + 7200)
            assert len(hooks.for_call(call_id)) == 1
            await app2["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_retry_loop_survives_a_corrupt_outbox_entry():
    async def go():
        hooks = Hooks(fail=1)
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        try:
            app = server.create_app(_opts(fake), data_dir=tmp)
            await server.on_startup(app)
            for name in ("retry_task", "watchdog_task"):
                app[name].cancel()
            vdir = server.voicemail_dir(app)
            (vdir / "CAbroken0000000001.json").write_text(json.dumps({"state": "final", "ha_post": {"transcript": {"attempts": "x"}}}))
            (vdir / "CAbroken0000000002.json").write_text("{not json")
            session = server.CallSession(app, "CAgood000000000001", {"from": FROM})
            app["sessions"][session.call_id] = session
            await session.finalize("stop")
            await server.retry_due(app, now=time.time() + 3600)
            assert len(hooks.for_call("CAgood000000000001")) == 1
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_persist_failure_still_delivers_transcript():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        original = server.persist_call
        try:
            async def boom(_session):
                raise OSError("disk full")
            server.persist_call = boom
            app = server.create_app(_opts(fake), data_dir=tmp)
            await server.on_startup(app)
            for name in ("retry_task", "watchdog_task"):
                app[name].cancel()
            session = server.CallSession(app, "CApersist000000001", {"from": FROM})
            app["sessions"][session.call_id] = session
            session.add_turn("caller", "Your car is ready.", final=True)
            await session.finalize("stop")
            got = hooks.for_call("CApersist000000001")
            assert len(got) == 1 and "Your car is ready." in got[0]["transcript_text"]
            assert got[0]["persist_error"] is True
            await app["client"].close()
        finally:
            server.persist_call = original
            await fake.close()

    _run(go())


def test_finalize_is_idempotent_under_concurrent_paths():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        try:
            app = server.create_app(_opts(fake), data_dir=tmp)
            await server.on_startup(app)
            for name in ("retry_task", "watchdog_task"):
                app[name].cancel()
            session = server.CallSession(app, "CAconcur0000000001", {"from": FROM})
            app["sessions"][session.call_id] = session
            await asyncio.gather(session.finalize("stop"), session.finalize("disconnect"),
                                 session.finalize("status:completed"), server.watchdog_tick(app, now=time.monotonic() + 9999))
            await server.record_missed(app, session.call_id, {"From": FROM}, "status:completed")
            assert len(hooks.for_call("CAconcur0000000001")) == 1
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_zero_second_call_and_unconfigured_webhooks():
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app({"xai_bridge": "stub", "log_level": "error"}, data_dir=tmp)
        await server.on_startup(app)
        for name in ("retry_task", "watchdog_task"):
            app[name].cancel()
        await server.record_missed(app, "CAzero000000000001", {"From": FROM}, "status:no-answer")
        side = server.load_sidecar(app, "CAzero000000000001")
        assert side["hangup"]["phase"] == "before_stream" and side["early_hangup"] is True
        assert side["ha_post"] == {"skipped": True}
        await server.retry_due(app, now=time.time() + 60)  # skipped is not pending
        await app["client"].close()

    _run(go())


class SlowHooks(Hooks):
    """HA webhook that takes `delay` seconds to answer (slow HA / restart)."""

    def __init__(self, delay=0.5):
        super().__init__()
        self.delay = delay

    async def handle(self, request):
        await asyncio.sleep(self.delay)
        return await super().handle(request)


def _bare_app(fake, tmp):
    async def go():
        app = server.create_app(_opts(fake), data_dir=tmp)
        await server.on_startup(app)
        for name in ("retry_task", "watchdog_task"):
            app[name].cancel()
        return app
    return go()


def test_slow_webhook_missed_call_races_retry_delivers_once():
    """MA M1: record_missed posts while the outbox retry pass runs."""
    async def go():
        hooks = SlowHooks(delay=0.5)
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        call_id = "CAslowmissed000001"
        try:
            app = await _bare_app(fake, tmp)
            missed = asyncio.create_task(server.record_missed(app, call_id, {"From": FROM}, "status:no-answer"))
            await asyncio.sleep(0.1)  # first post is in flight
            side = server.load_sidecar(app, call_id)
            assert side["next_retry_at"] > time.time()  # claimed before the first post
            await server.retry_due(app)  # races the in-flight post
            await server.retry_due(app, now=time.time() + 3600)  # even if "due"
            await missed
            await server.retry_due(app, now=time.time() + 3600)
            assert hooks.calls == 1 and len(hooks.for_call(call_id)) == 1
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_slow_webhook_recovery_races_retry_and_watchdog_delivers_once():
    """MA M1: orphan recovery posts while retry and a second recovery run."""
    async def go():
        hooks = SlowHooks(delay=0.5)
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        call_id = "CAslowrecover00001"
        try:
            app = await _bare_app(fake, tmp)
            session = server.CallSession(app, call_id, {"from": FROM})
            session.add_turn("caller", "Slow HA, call me.", final=True)
            session.checkpoint()  # orphan journal, no live session
            rec = asyncio.create_task(server.recover_orphans(app))
            await asyncio.sleep(0.1)
            await asyncio.gather(
                server.retry_due(app, now=time.time() + 3600),
                server.recover_orphans(app),
                server.record_missed(app, call_id, {"From": FROM}, "status:completed"),
            )
            await rec
            await server.retry_due(app, now=time.time() + 3600)
            got = hooks.for_call(call_id)
            assert hooks.calls == 1 and len(got) == 1 and "Slow HA" in got[0]["transcript_text"]
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_prune_removes_only_old_acked_sidecars():
    async def go():
        hooks = Hooks()
        fake = await _fake(hooks)
        tmp = tempfile.mkdtemp()
        try:
            app = await _bare_app(fake, tmp)
            vdir = server.voicemail_dir(app)
            await server.record_missed(app, "CAprunacked0000001", {"From": FROM}, "status:no-answer")
            (vdir / "CAprunacked0000001.wav").write_bytes(b"RIFF")
            (vdir / "CAprunpending00001.json").write_text(json.dumps({"state": "final", "call_id": "CAprunpending00001", "ha_post": {"transcript": {"ok": False, "attempts": 1}}}))
            (vdir / "CAprunopen00000001.json").write_text(json.dumps({"state": "open", "turns": []}))
            await server.record_missed(app, "CAprunfresh0000001", {"From": FROM}, "status:no-answer")
            old = time.time() - server.SIDECAR_RETENTION_S - 60
            for name in ("CAprunacked0000001", "CAprunpending00001", "CAprunopen00000001"):
                os.utime(vdir / f"{name}.json", (old, old))
            assert server.prune_sidecars(app) == 1
            assert not (vdir / "CAprunacked0000001.json").exists()
            assert not (vdir / "CAprunacked0000001.wav").exists()
            for keep in ("CAprunpending00001", "CAprunopen00000001", "CAprunfresh0000001", "index"):
                assert (vdir / f"{keep}.json").exists(), keep
            # The watchdog runs the prune at most once per interval.
            app["runtime"].last_prune = None
            await server.watchdog_tick(app)
            assert app["runtime"].last_prune > 0
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("OK")
