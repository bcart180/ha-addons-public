"""0.2.6: HA readiness probe (no Core API), hard attempt cap + ack_timeout,
posted_unready wedge guard, runtime state holder.

Run: python3 twilio_house_voice/tests/test_ready.py  (or pytest)
"""
import asyncio
import logging
import os
import pathlib
import re
import sys
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestServer

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(APP))
os.environ.pop("SUPERVISOR_TOKEN", None)

import server  # noqa: E402

FROM = "+15555550100"
AUTH = "twilio-auth-token-value"
HOOK_ID = "cc_hook_7f3a9e2b41d04c55"
WEBHOOK_TOKEN = "webhook-token-value"


class FakeHA:
    """Mimics HA's webhook view (components/webhook/__init__.py):
    unregistered id -> 200 for any method, body dropped; registered POST-only
    hook -> GET 405, POST runs the automation; down -> 503."""

    def __init__(self, mode="registered", post_status=200):
        self.mode = mode
        self.post_status = post_status
        self.gets = []
        self.delivered = []
        self.dropped = 0

    async def handle(self, request):
        if self.mode == "down":
            return web.Response(status=503)
        if request.method == "GET":
            self.gets.append(dict(request.headers))
            return web.Response(status=405 if self.mode == "registered" else 200)
        body = await request.json()
        if self.mode == "unregistered":
            self.dropped += 1
            return web.Response(status=200)
        if self.post_status != 200:
            return web.Response(status=self.post_status)
        self.delivered.append(body)
        return web.Response(status=200)


async def _fake(ha):
    app = web.Application()
    app.router.add_route("*", "/api/webhook/{hook_id}", ha.handle)
    app.router.add_route("*", "/hook", ha.handle)
    fake = TestServer(app)
    await fake.start_server()
    return fake


async def _app(fake, path=f"/api/webhook/{HOOK_ID}"):
    opts = {
        "xai_bridge": "stub",
        "public_base_url": "https://voice.example",
        "twilio_auth_token": AUTH,
        "ha_webhook_transcript": str(fake.make_url(path)),
        "ha_webhook_token": WEBHOOK_TOKEN,
        "log_level": "error",
    }
    app = server.create_app(opts, data_dir=tempfile.mkdtemp())
    await server.on_startup(app)
    for name in ("retry_task", "watchdog_task"):
        app[name].cancel()
    return app


def _run(coro):
    asyncio.run(coro)


class Knobs:
    """Temporarily shorten timing constants."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = getattr(server, k)
            setattr(server, k, v)

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(server, k, v)


def test_backoff_schedule_and_hard_cap():
    assert server.WEBHOOK_ATTEMPT_CAP == 10
    delays = [server._next_retry_delay(n) for n in range(1, 13)]
    assert delays[:10] == [30, 60, 120, 240, 480, 900, 1800, 3600, 7200, 14400]
    assert delays[10] == delays[11] == 14400
    span = sum(delays[: server.WEBHOOK_ATTEMPT_CAP - 1])
    assert 3.5 * 3600 < span < server.OUTBOX_MAX_AGE_S  # 10 posts span ~4 h of backoff, inside 24 h


def test_restart_unregistered_hook_holds_then_settles_then_delivers_once():
    """The 12:01 pattern: HA answers 200 before the automation is loaded."""
    async def go():
        ha = FakeHA(mode="unregistered")
        fake = await _fake(ha)
        call_id = "CA" + "a" * 32
        try:
            with Knobs(HA_SETTLE_S=0.3):
                app = await _app(fake)
                await server.record_missed(app, call_id, {"From": FROM}, "status:no-answer")
                side = server.load_sidecar(app, call_id)
                row = side["ha_post"]["transcript"]
                assert row["error"] == "ha_not_ready" and row["ready"] == "unregistered"
                assert row["attempts"] == 0 and ha.dropped == 0 and not ha.delivered
                assert side["next_retry_at"] - time.time() <= server.HA_HOLD_BACKOFF_S[0] + 1
                ha.mode = "down"
                await server.retry_due(app, now=time.time() + 3600)
                assert server.load_sidecar(app, call_id)["ha_post"]["transcript"]["ready"] == "down"
                ha.mode = "registered"
                await server.retry_due(app, now=time.time() + 3600)
                row = server.load_sidecar(app, call_id)["ha_post"]["transcript"]
                assert row["ready"] == "settling" and not ha.delivered
                await asyncio.sleep(0.35)
                await server.retry_due(app, now=time.time() + 3600)
                await server.retry_due(app, now=time.time() + 7200)
                assert len(ha.delivered) == 1 and ha.dropped == 0
                row = server.load_sidecar(app, call_id)["ha_post"]["transcript"]
                assert row["ok"] and row["attempts"] == 1 and "posted_unready" not in row
                await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_steady_state_has_no_settle_delay_and_probe_is_safe():
    async def go():
        ha = FakeHA(mode="registered")
        fake = await _fake(ha)
        call_id = "CA" + "b" * 32
        logged = []

        class Grab(logging.Handler):
            def emit(self, record):
                logged.append(record.getMessage())

        handler = Grab()
        server.log.addHandler(handler)
        old = server.log.level
        server.log.setLevel(logging.DEBUG)
        try:
            app = await _app(fake)
            await server.record_missed(app, call_id, {"From": FROM}, "status:no-answer")
            assert len(ha.delivered) == 1 and len(ha.gets) == 1
            probe = ha.gets[0]
            assert "Authorization" not in probe and "X-Webhook-Token" not in probe
            text = "\n".join(logged)
            assert HOOK_ID not in text and WEBHOOK_TOKEN not in text and FROM not in text, text
            await app["client"].close()
        finally:
            server.log.removeHandler(handler)
            server.log.setLevel(old)
            await fake.close()

    _run(go())


def test_non_ha_webhook_is_not_probed():
    async def go():
        ha = FakeHA(mode="registered")
        fake = await _fake(ha)
        try:
            app = await _app(fake, path="/hook")
            assert not server.ha_probe_applies(app["opts"].ha_webhook_transcript)
            assert server.ha_probe_applies("http://homeassistant:8123/api/webhook/x")
            assert not server.ha_probe_applies("https://hooks.nabu.casa/abc")
            await server.record_missed(app, "CA" + "c" * 32, {"From": FROM}, "status:no-answer")
            assert len(ha.delivered) == 1 and not ha.gets
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_stuck_readiness_posts_anyway_after_deadline_flagged_posted_unready():
    async def go():
        ha = FakeHA(mode="unregistered")
        fake = await _fake(ha)
        call_id = "CA" + "d" * 32
        try:
            with Knobs(HA_UNREADY_MAX_S=0.2):
                app = await _app(fake)
                await server.record_missed(app, call_id, {"From": FROM}, "status:no-answer")
                assert ha.dropped == 0
                await asyncio.sleep(0.25)
                await server.retry_due(app, now=time.time() + 3600)
                assert ha.dropped == 1  # best-effort post went out
                row = server.load_sidecar(app, call_id)["ha_post"]["transcript"]
                assert row["posted_unready"] is True and row["attempts"] == 1
                index = server.load_index(app)
                hit = [r for r in index if r.get("call_id") == call_id]
                assert hit and hit[0]["posted_unready"] is True
                await server.retry_due(app, now=time.time() + 7200)
                assert ha.dropped == 1  # 2xx: not re-posted in Phase 1
                await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_attempt_cap_marks_ack_timeout_once_and_stops():
    async def go():
        ha = FakeHA(mode="registered", post_status=500)
        fake = await _fake(ha)
        call_id = "CA" + "e" * 32
        try:
            with Knobs(WEBHOOK_ATTEMPT_CAP=3):
                app = await _app(fake)
                real_sleep = asyncio.sleep

                async def fast_sleep(_s):
                    await real_sleep(0)

                server.asyncio.sleep = fast_sleep
                try:
                    await server.record_missed(app, call_id, {"From": FROM}, "status:no-answer")
                    for _ in range(5):
                        await server.retry_due(app, now=time.time() + 50000)
                finally:
                    server.asyncio.sleep = real_sleep
                side = server.load_sidecar(app, call_id)
                assert side["ha_post"]["transcript"]["attempts"] == 3
                assert side["ack_timeout"]["reason"] == "attempt_cap"
                assert "next_retry_at" not in side
                assert not server._pending_record(side)
                stamp = side["ack_timeout"]["at"]
                await server.retry_due(app, now=time.time() + 60000)
                assert server.load_sidecar(app, call_id)["ack_timeout"]["at"] == stamp
                body = server.webhook_body(side, False, app["opts"])
                assert "ack_timeout" not in body and "ha_post" not in body
                await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_max_age_marks_ack_timeout():
    async def go():
        ha = FakeHA(mode="down")
        fake = await _fake(ha)
        call_id = "CA" + "f" * 32
        try:
            app = await _app(fake)
            await server.record_missed(app, call_id, {"From": FROM}, "status:no-answer")
            await server.retry_due(app, now=time.time() + server.OUTBOX_MAX_AGE_S + 60)
            side = server.load_sidecar(app, call_id)
            assert side["ack_timeout"]["reason"] == "max_age" and not ha.delivered
            await app["client"].close()
        finally:
            await fake.close()

    _run(go())


def test_runtime_state_holder_no_app_mutation_after_start():
    app = server.create_app({"log_level": "error"}, data_dir=tempfile.mkdtemp())
    rt = app["runtime"]
    assert rt.last_prune is None and rt.inflight == set() and rt.ha_ready == {}
    assert server._inflight(app) is rt.inflight
    src = (APP / "server.py").read_text()
    assert 'app["last_prune"]' not in src and 'setdefault("inflight"' not in src
    # app[...] = ... only in create_app / on_startup
    func = None
    bad = []
    for line in src.splitlines():
        m = re.match(r"(?:async )?def (\w+)", line)
        if m:
            func = m.group(1)
        if re.search(r'\bapp\["\w+"\]\s*=[^=]', line) and func not in ("create_app", "on_startup"):
            bad.append((func, line.strip()))
    assert not bad, bad


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("OK")


class GetEnabledHA(FakeHA):
    """A misconfigured target whose GET runs an automation and returns a body."""

    def __init__(self, body=b"", ctype="application/json"):
        super().__init__()
        self.body = body
        self.ctype = ctype

    async def handle(self, request):
        if request.method == "GET":
            self.gets.append(dict(request.headers))
            return web.Response(status=200, body=self.body, content_type=self.ctype)
        self.delivered.append(await request.json())
        return web.Response(status=200)


def test_l2_non_ha_200_is_probe_unsupported_and_posts_without_holding():
    async def go():
        for body, ctype in ((b'{"ok":true}', "application/json"), (b"", "text/html")):
            ha = GetEnabledHA(body=body, ctype=ctype)
            fake = await _fake(ha)
            try:
                app = await _app(fake)
                assert await server.ha_probe(app, str(fake.make_url(f"/api/webhook/{HOOK_ID}"))) == "unsupported"
                call_id = "CA" + "b" * 32
                await server.record_missed(app, call_id, {"From": FROM}, "status:no-answer")
                row = server.load_sidecar(app, call_id)["ha_post"]["transcript"]
                assert row["ok"] and row["attempts"] == 1 and len(ha.delivered) == 1
                assert len(ha.gets) == 2  # one direct probe above + one before the post, no hold loop
            finally:
                await fake.close()
    _run(go())


def test_l2_empty_200_is_still_unregistered():
    async def go():
        ha = FakeHA(mode="unregistered")
        fake = await _fake(ha)
        try:
            app = await _app(fake)
            assert await server.ha_probe(app, str(fake.make_url(f"/api/webhook/{HOOK_ID}"))) == "unregistered"
        finally:
            await fake.close()
    _run(go())


def test_l3_healthy_posts_reuse_ready_cache_and_failure_forces_probe():
    async def go():
        ha = FakeHA(mode="registered")
        fake = await _fake(ha)
        try:
            app = await _app(fake)
            for i in range(3):
                await server.record_missed(app, "CA" + str(i) * 32, {"From": FROM}, "status:no-answer")
            assert len(ha.delivered) == 3 and len(ha.gets) == 1  # only the first call probed
            ha.post_status = 500
            await server.record_missed(app, "CA" + "3" * 32, {"From": FROM}, "status:no-answer")
            assert len(ha.gets) == 1  # still cached; the post itself fails
            ha.post_status = 200
            await server.record_missed(app, "CA" + "4" * 32, {"From": FROM}, "status:no-answer")
            assert len(ha.gets) == 2  # failure cleared the cache, so this one probed
            with Knobs(HA_READY_TTL_S=0):
                await server.record_missed(app, "CA" + "5" * 32, {"From": FROM}, "status:no-answer")
            assert len(ha.gets) == 3  # cache expired -> probe
        finally:
            await fake.close()
    _run(go())


def test_l3_hold_backoff_schedule():
    assert [server._hold_delay(n) for n in range(1, 7)] == [15, 30, 60, 120, 120, 120]
    # ~10 min hold window costs far fewer than the old ~20 probes
    t, n = 0, 0
    while t < server.HA_UNREADY_MAX_S:
        n += 1
        t += server._hold_delay(n)
    assert n <= 8
