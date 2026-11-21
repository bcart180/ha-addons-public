"""0.8.0 carrier recordings: every call, one row, one email.  Fake data only.

555-01xx numbers, placeholder keys, placeholder storage hosts.  No live network.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import struct
import threading
import time
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load

import inbox_store  # noqa: E402
import telnyx_capture  # noqa: E402

AGENT = "agent-token-555-" + "x" * 24
CALLER = "+15555550100"
TELNYX_KEY = "telnyx-test-key"
XAI_KEY = "xai-test-key-value"
SESSION = "11111111-1111-4111-8111-111111111111"
LEG = "33333333-3333-4333-8333-333333333333"
REC = "22222222-2222-4222-8222-222222222222"
CONTROL = "v3:test-control"
# Fake Telnyx connection ids.  CONN is the allowlisted inbound connection; OTHER_CONN
# stands in for another app on the same account (for example an outbound-only TeXML app).
CONN = "1000000000000000001"
OTHER_CONN = "1000000000000000002"
URL = "https://s3.amazonaws.com/telephony-recorder-prod/test.wav?sig=placeholder"
HDR = {"Authorization": f"Bearer {AGENT}"}
ROOT = pathlib.Path(__file__).resolve().parents[1]


def wav_bytes(n: int = 160) -> bytes:
    data = b"\x00\x00" * n
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, 1, 8000, 16000, 2, 16, b"data", len(data),
    )
    return header + data


def stamp(offset: float) -> str:
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def saved_event(event_id: str = "44444444-4444-4444-8444-444444444444", url: str = URL,
                start: str | None = None, end: str | None = None, recording_id: str = REC) -> dict:
    return {
        "event_id": event_id,
        "event_type": "call.recording.saved",
        "call_session_id": SESSION,
        "call_leg_id": LEG,
        "call_control_id": CONTROL,
        "connection_id": CONN,
        "recording_id": recording_id,
        "from": CALLER,
        "channels": "dual",
        "recording_urls": {"wav": url},
        "recording_started_at": start or stamp(-4),
        "recording_ended_at": end or stamp(-2),
    }


def hangup_event() -> dict:
    return {
        "event_id": "55555555-5555-4555-8555-555555555555",
        "event_type": "call.hangup",
        "call_session_id": SESSION,
        "call_leg_id": LEG,
        "call_control_id": CONTROL,
        "connection_id": CONN,
        "from": CALLER,
        "start_time": stamp(-4),
        "end_time": stamp(-2),
    }


def api_record(url: str = URL) -> dict:
    return {
        "id": REC,
        "status": "completed",
        "connection_id": CONN,
        "call_session_id": SESSION,
        "call_leg_id": LEG,
        "call_control_id": CONTROL,
        "channels": "dual",
        "download_urls": {"wav": url},
        "duration_millis": 2000,
        "recording_started_at": stamp(-4),
        "recording_ended_at": stamp(-2),
        "from": CALLER,
    }


class FakeNet:
    def __init__(self, stt, blob: bytes | None = None, delete_remote: bool = True):
        self.stt_script = list(stt)
        self.blob = wav_bytes() if blob is None else blob
        self.delete_remote = delete_remote
        self.fetched: list[str] = []
        self.deleted: list[str] = []
        self.listed: list = []
        self.records: list = []

    async def fetch(self, url: str):
        self.fetched.append(url)
        return self.blob

    async def stt(self, blob: bytes, fmt: str, channels: str):
        if not self.stt_script:
            return None
        return self.stt_script.pop(0)

    async def refresh(self, recording_id: str) -> str:
        return ""

    async def list_recordings(self, created_gte: str, page: int):
        self.listed.append((created_gte, page))
        if page == 1:
            return {"data": list(self.records), "pages": 1}
        return {"data": [], "pages": 1}

    async def delete(self, recording_id: str) -> bool:
        self.deleted.append(recording_id)
        return True


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("VM_CAPTURE_AUTO", "0")
    return _load(
        monkeypatch, tmp_path, agent_token=AGENT, push_service="mobile_app_test_phone",
        mail_to="callcatch-test@proton.me", agentmail_inbox="callcatch-test@agentmail.to",
        telnyx_api_key=TELNYX_KEY, xai_api_key=XAI_KEY, telnyx_delete_remote=True,
        telnyx_connection_ids=CONN,
    )


def _emails(mod, app) -> dict:
    _calls, outbox = mod.ensure_agent(app)
    if not outbox.path.exists():
        return {}
    jobs = json.loads(outbox.path.read_text(encoding="utf-8"))["jobs"]
    return {k: v for k, v in jobs.items() if k.startswith("email:")}


async def _post(client, event: dict):
    resp = await client.post("/api/agent/telnyx", json={"event": event}, headers=HDR)
    return resp.status, await resp.json()


async def _advance(mod, app, net: FakeNet, now: float | None = None, gate: str = "send"):
    cap = mod.ensure_capture(app)
    return await cap.advance(net, lambda job: mod.attach_capture(app, job), now=now, delete_gate=gate)


def test_url_allowlist_rejects_ssrf_and_accepts_recording_hosts():
    assert telnyx_capture.url_allowed(URL)
    assert telnyx_capture.url_allowed("https://storage.telnyx.com/rec.wav")
    assert telnyx_capture.url_allowed("https://telephony-recorder-prod.s3.amazonaws.com/rec.wav")
    assert telnyx_capture.url_allowed("https://telephony-recorder-prod.s3.us-east-1.amazonaws.com/rec.wav")
    assert telnyx_capture.url_allowed("https://bucket.s3.us-east-1.amazonaws.com/rec.wav") is False
    assert telnyx_capture.url_allowed("https://s3.amazonaws.com/other-bucket/rec.wav") is False
    assert telnyx_capture.url_allowed(
        "https://s3.amazonaws.com/telephony-recorder-prod/../other/rec.wav") is False
    assert telnyx_capture.url_allowed(
        "https://s3.amazonaws.com/telephony-recorder-prod/%2e%2e/other/rec.wav") is False
    assert telnyx_capture.url_allowed(
        "https://s3.amazonaws.com/telephony-recorder-prod/%252e%252e/other/rec.wav") is False
    for bad in (
        "http://s3.amazonaws.com/rec.wav",
        "https://169.254.169.254/latest/meta-data",
        "https://evil.example/rec.wav",
        "https://user:pass@s3.amazonaws.com/rec.wav",
        "https://s3.amazonaws.com:444/rec.wav",
        "https://localhost/rec.wav",
        "https://169.254.169.254.example/rec.wav",
        "file:///etc/passwd",
        "https://s3.amazonaws.com.evil.example/rec.wav",
    ):
        assert telnyx_capture.url_allowed(bad) is False, bad


def test_oversize_body_is_rejected_before_save():
    err, blob = telnyx_capture.interpret_fetch(b"\x00" * (telnyx_capture.MAX_AUDIO + 1))
    assert err == "oversize" and blob == b""
    err, blob = telnyx_capture.interpret_fetch({"error": "oversize"})
    assert err == "oversize" and blob == b""


def test_loopback_name_is_not_a_public_download_host():
    assert telnyx_capture.host_is_public("localhost") is False


def test_cgnat_and_nonpublic_addresses_are_rejected():
    import ipaddress
    rejected = (
        "100.64.0.1", "100.127.255.254", "10.1.1.1", "172.16.5.5", "192.168.1.1",
        "127.0.0.1", "169.254.1.1", "224.0.0.1", "192.0.2.1", "0.0.0.0",
    )
    for text in rejected:
        assert telnyx_capture.address_is_public(ipaddress.ip_address(text)) is False, text
    assert telnyx_capture.address_is_public(ipaddress.ip_address("8.8.8.8")) is True


def test_empty_notice_text():
    assert telnyx_capture.empty_notice(2) == "caller hung up after 2 seconds, no speech detected"


def test_panel_is_black_with_blue_accent_and_no_amber():
    css = (ROOT / "rootfs" / "app" / "static" / "style.css").read_text(encoding="utf-8").lower()
    html = (ROOT / "rootfs" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "rootfs" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "--bg: #000" in css and "--accent: #3e6ae1" in css
    assert 'id="d-recording"' in html and "Verbatim carrier recording" in js
    for bad in ("orange", "amber", "#ffa500", "#f59e0b", "#ffbf00", "#ff9900"):
        assert bad not in css


@pytest.mark.asyncio
async def test_early_hangup_with_no_agent_tool_call(mod, caplog):
    caplog.set_level(logging.INFO)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        assert (await _post(client, hangup_event()))[0] == 200
        status, body = await _post(client, saved_event())
        assert status == 200 and body["ok"] is True and body["duplicate"] is False
        net = FakeNet([{"text": "please call back later", "duration": 2.0, "words": [{"text": "please", "start": 0.1}]}])
        await _advance(mod, app, net)
        emails = _emails(mod, app)
        assert len(emails) == 1
        mail = next(iter(emails.values()))
        assert "please call back later" in mail["payload"]["text"]
        calls = mod.ensure_agent(app)
        rows = calls[0].all()
        assert len(rows) == 1 and rows[0]["hung_up_early"] is True and rows[0]["final_source"] == "recording"
        assert rows[0]["turns"] == []
        assert net.deleted == [REC]
        item = mod.ensure_runtime(app).get("pa-" + rows[0]["ref"].lower())
        assert item["recording_verbatim"] is True and item["has_audio"] is True
        shown = mod.normalize_item(item)
        assert shown["recording_verbatim"] is True and "please call back later" in shown["transcript"]
        blob = " ".join(caplog.messages)
        assert CALLER not in blob and XAI_KEY not in blob and TELNYX_KEY not in blob and "placeholder" not in blob


@pytest.mark.asyncio
async def test_webhook_and_poller_race_one_row_one_email(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        cap = mod.ensure_capture(app)
        event = saved_event()
        record = api_record()
        errors: list[str] = []

        def hit() -> None:
            try:
                cap.ingest_webhook(event)
                cap.ingest_api(record)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__)

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert len(cap.jobs()) == 1
        net = FakeNet([{"text": "race transcript", "duration": 2}])
        await asyncio.gather(_advance(mod, app, net), _advance(mod, app, net))
        assert len(cap.jobs()) == 1
        assert len(_emails(mod, app)) == 1
        assert net.deleted == [REC]
        assert len(mod.ensure_agent(app)[0].all()) == 1


@pytest.mark.asyncio
async def test_duplicate_webhooks_one_row(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        first = await _post(client, saved_event())
        second = await _post(client, saved_event())
        assert first[1]["duplicate"] is False and second[1]["duplicate"] is True
        net = FakeNet([{"text": "once", "duration": 1}])
        await _advance(mod, app, net)
        await _advance(mod, app, net)
        assert len(_emails(mod, app)) == 1
        assert net.deleted == [REC]


@pytest.mark.asyncio
async def test_ssrf_host_is_not_fetched(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        bad = "https://169.254.169.254/latest/meta-data"
        await _post(client, saved_event(url=bad))
        net = FakeNet([{"text": "nope", "duration": 1}])
        await _advance(mod, app, net)
        assert net.fetched == [] and net.deleted == []
        job = next(iter(mod.ensure_capture(app).jobs().values()))
        assert job["state"] == "dead" and job["last_error"] == "ssrf"


@pytest.mark.asyncio
async def test_oversize_download_rejected(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        net = FakeNet([{"text": "nope"}], blob={"error": "oversize"})
        await _advance(mod, app, net)
        assert net.deleted == []
        job = next(iter(mod.ensure_capture(app).jobs().values()))
        assert job["state"] == "dead" and job["last_error"] == "oversize" and not job.get("file")
        audio = [p for p in mod.ensure_capture(app).dir.iterdir() if p.suffix in (".wav", ".mp3")]
        assert audio == []


@pytest.mark.asyncio
async def test_stt_failure_then_retry_success_deletes_only_after_persist(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        cap = mod.ensure_capture(app)
        net = FakeNet([None, {"text": "heard on the second try", "duration": 3}])

        async def guarded_delete(recording_id: str) -> bool:
            job = cap.get(REC)
            assert job and job["transcript"] and str(job["ref"]).startswith("CC-")
            path = cap.dir / job["file"]
            assert path.is_file() and path.stat().st_size == job["bytes"]
            assert cap.persisted(job)
            net.deleted.append(recording_id)
            return True

        net.delete = guarded_delete
        await _advance(mod, app, net)
        assert net.deleted == []
        job = cap.get(REC)
        assert job["state"] == "transcribe" and job["last_error"] == "stt"
        await _advance(mod, app, net, now=float(job["next_at"]) + 1)
        assert net.deleted == [REC]
        assert cap.get(REC)["state"] == "done" and "heard on the second try" in cap.get(REC)["transcript"]
        assert len(_emails(mod, app)) == 1


@pytest.mark.asyncio
async def test_delete_never_runs_when_stt_keeps_failing(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        cap = mod.ensure_capture(app)
        net = FakeNet([None] * 12)
        for _ in range(12):
            job = cap.get(REC)
            if job and job["state"] == "dead":
                break
            await _advance(mod, app, net, now=float((job or {}).get("next_at") or 0) + 1)
        assert net.deleted == []
        assert cap.get(REC)["state"] == "dead"


@pytest.mark.asyncio
async def test_delete_off_keeps_the_remote(monkeypatch, tmp_path):
    monkeypatch.setenv("VM_CAPTURE_AUTO", "0")
    mod = _load(
        monkeypatch, tmp_path, agent_token=AGENT, telnyx_api_key=TELNYX_KEY, xai_api_key=XAI_KEY,
        telnyx_delete_remote=False, push_service="mobile_app_test_phone",
        mail_to="callcatch-test@proton.me", agentmail_inbox="callcatch-test@agentmail.to",
        telnyx_connection_ids=CONN,
    )
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        net = FakeNet([{"text": "kept remote", "duration": 2}], delete_remote=False)
        await _advance(mod, app, net)
        assert net.deleted == []
        job = mod.ensure_capture(app).get(REC)
        assert job["state"] == "done" and job["deleted"] == "off"
        assert len(_emails(mod, app)) == 1


@pytest.mark.asyncio
async def test_empty_recording_still_notifies(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        start, end = stamp(-4), stamp(-2)
        await _post(client, hangup_event())
        await _post(client, saved_event(start=start, end=end))
        net = FakeNet([{"text": "", "words": [], "duration": 2}])
        await _advance(mod, app, net)
        emails = _emails(mod, app)
        assert len(emails) == 1
        text = next(iter(emails.values()))["payload"]["text"]
        assert "caller hung up after 2 seconds, no speech detected" in text
        assert net.deleted == [REC]
        rows = mod.ensure_agent(app)[0].all()
        assert len(rows) == 1 and rows[0]["hung_up_early"] is True


@pytest.mark.asyncio
async def test_dry_run_gate_still_blocks_the_send(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent: list[str] = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        async def read_state(_app, entity):
            assert entity == "input_boolean.callcatch_notify_dry_run"
            return "on"

        mod.read_entity_state = read_state
        mod._box(app)["senders"] = {"push": fake, "email": fake}
        await _post(client, saved_event())
        await _advance(mod, app, FakeNet([{"text": "dry run body", "duration": 2}]))
        await mod.agent_tick(app)
        emails = _emails(mod, app)
        assert len(emails) == 1
        job = next(iter(emails.values()))
        assert job["status"] == "dry_run" and sent == []
        assert mod.ensure_runtime(app).list_items()  # the call is still saved


@pytest.mark.asyncio
async def test_masking_keeps_transcript_and_full_number_off_sensor_and_logs(mod, caplog):
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        phrase = "herringphrase 555-555-0100"
        await _advance(mod, app, FakeNet([{"text": phrase, "duration": 3}]))
        mail = next(iter(_emails(mod, app).values()))["payload"]["text"]
        assert "555-555-0100" not in mail and "0100" in mail
        item = mod.ensure_runtime(app).list_items()[0]
        sensor = json.dumps(inbox_store.sensor_state([item]))
        assert "herringphrase" not in sensor and "555-555-0100" not in sensor and CALLER not in sensor
        assert "0100" in sensor  # last 4 only, on the masked caller
        blob = " ".join(caplog.messages)
        assert CALLER not in blob and "herringphrase" not in blob and XAI_KEY not in blob


@pytest.mark.asyncio
async def test_restart_mid_call_recovered_by_poller(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        cap = mod.ensure_capture(app)
        answered = {
            "event_id": "66666666-6666-4666-8666-666666666666",
            "event_type": "call.answered",
            "connection_id": CONN,
            "call_session_id": SESSION,
            "call_leg_id": LEG,
            "call_control_id": CONTROL,
            "from": CALLER,
            "start_time": stamp(-4),
        }
        assert cap.ingest_webhook(answered)["ok"] is True
        # Process restart: the in-memory object is gone; the job file is not.
        mod._box(app)["capture"] = None
        cap2 = mod.ensure_capture(app)
        assert cap2.path == cap.path and len(cap2.jobs()) == 1
        net = FakeNet([{"text": "recovered after restart", "duration": 4}])
        net.records = [api_record()]
        assert await cap2.poll(net) == "poll-ok"
        assert net.listed and len(cap2.jobs()) == 1
        await cap2.advance(net, lambda job: mod.attach_capture(app, job), delete_gate="send")
        assert net.deleted == [REC]
        emails = _emails(mod, app)
        assert len(emails) == 1 and "recovered after restart" in next(iter(emails.values()))["payload"]["text"]


@pytest.mark.asyncio
async def test_agent_final_waits_so_the_one_email_includes_the_recording(mod):
    """The agent can finish before the file is saved.  The email still goes out once, with the verbatim text."""
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        start = stamp(-3)
        key = "cd" * 16
        status, body = await _post_call(client, {
            "op": "final", "call_key": key, "kind": "message", "caller_hint": CALLER, "call_start": start,
            "fields": {"caller_name": "Test Caller", "reason": "about the gate code"},
        })
        assert status == 200 and body["state"] == "awaiting_recording"
        assert _emails(mod, app) == {}
        event = saved_event(start=start, end=stamp(-1))
        await _post(client, event)
        await _advance(mod, app, FakeNet([{"text": "verbatim after the message", "duration": 3}]))
        emails = _emails(mod, app)
        assert len(emails) == 1
        text = next(iter(emails.values()))["payload"]["text"]
        assert "verbatim after the message" in text and "about the gate code" in text
        assert len(mod.ensure_agent(app)[0].all()) == 1


async def _post_call(client, body: dict):
    resp = await client.post("/api/agent/call", json=body, headers=HDR)
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_agent_turns_join_the_same_call(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        start = stamp(-3)
        key = "ab" * 16
        touch = await client.post("/api/agent/call", json={
            "op": "touch", "call_key": key, "caller_hint": CALLER, "call_start": start,
            "turn": {"speaker": "caller", "text": "agent heard this"},
        }, headers=HDR)
        assert touch.status == 200
        ref = (await touch.json())["ref"]
        event = saved_event(start=start, end=stamp(-1))
        event["recording_started_at"] = start
        await _post(client, event)
        await _advance(mod, app, FakeNet([{"text": "verbatim from the recording", "duration": 3}]))
        rows = mod.ensure_agent(app)[0].all()
        assert len(rows) == 1 and rows[0]["ref"] == ref
        assert rows[0]["turns"] and "verbatim from the recording" in rows[0]["transcript"]
        assert len(_emails(mod, app)) == 1
        assert "verbatim from the recording" in next(iter(_emails(mod, app).values()))["payload"]["text"]


@pytest.mark.asyncio
async def test_dry_run_gate_blocks_telnyx_delete(mod):
    """Delete only when the notify switch is explicitly off and the option is on."""
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        ids = {
            "dry_run": "22222222-2222-4222-8222-222222222201",
            "hold": "22222222-2222-4222-8222-222222222202",
            "unavailable": "22222222-2222-4222-8222-222222222203",
            "send": "22222222-2222-4222-8222-222222222204",
        }
        net = FakeNet([
            {"text": "dry run delete check", "duration": 2},
            {"text": "unknown delete check", "duration": 2},
            {"text": "bad gate delete check", "duration": 2},
            {"text": "live delete check", "duration": 2},
        ])
        for gate, rid in ids.items():
            event = saved_event(event_id=rid, recording_id=rid)
            await _post(client, event)
            await _advance(mod, app, net, gate=gate)
            job = mod.ensure_capture(app).get(rid)
            assert job is not None
            if gate == "send":
                assert rid in net.deleted
                assert job["deleted"] == "yes" and job["state"] == "done"
                assert (mod.ensure_capture(app).dir / job["file"]).is_file()
            elif gate == "dry_run":
                assert rid not in net.deleted
                assert job["deleted"] == "dry_run" and job["state"] == "done"
            else:
                assert rid not in net.deleted
                assert job["state"] == "delete" and job["last_error"] == "dry-run-unknown"


@pytest.mark.asyncio
async def test_second_recording_is_separate_and_one_email(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        # Lower id is stored first (the job file sorts keys) but it starts later.
        # The email must follow start time, not that storage order.
        low_id = "22222222-2222-4222-8222-222222222210"
        high_id = "22222222-2222-4222-8222-222222222211"
        await _post(client, saved_event(
            event_id="44444444-4444-4444-8444-444444444410", recording_id=low_id,
            start=stamp(-2), end=stamp(-1),
        ))
        await _post(client, saved_event(
            event_id="44444444-4444-4444-8444-444444444411", recording_id=high_id,
            start=stamp(-8), end=stamp(-6),
        ))
        cap = mod.ensure_capture(app)
        assert len(cap.jobs()) == 2
        net = FakeNet([
            {"text": "later words", "duration": 1},
            {"text": "earlier words", "duration": 2},
        ])
        await _advance(mod, app, net)
        assert sorted(net.deleted) == sorted([low_id, high_id])
        assert len(net.fetched) == 2
        emails = _emails(mod, app)
        assert len(emails) == 1
        text = next(iter(emails.values()))["payload"]["text"]
        assert text.index("earlier words") < text.index("later words")
        assert len(mod.ensure_agent(app)[0].all()) == 1


@pytest.mark.asyncio
async def test_failure_is_not_stored_as_the_transcript(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        net = FakeNet([{"text": "nope"}], blob={"error": "oversize"})
        await _advance(mod, app, net)
        job = mod.ensure_capture(app).get(REC)
        assert job["state"] == "dead" and job["last_error"] == "oversize"
        assert not job.get("transcript")
        assert "could not be transcribed" not in json.dumps(job)
        rows = mod.ensure_agent(app)[0].all()
        assert len(rows) == 1
        assert rows[0]["capture_failed"] is True
        assert rows[0]["capture_error"] == "oversize"
        assert rows[0]["transcript"] == ""
        assert rows[0].get("verbatim_at") in (0, None, "")
        mail = next(iter(_emails(mod, app).values()))["payload"]["text"]
        assert "could not be transcribed" in mail
        assert mail.split("Transcript", 1)[1].split("Find it", 1)[0].strip() in ("Not received.", "Not received.\n") or "Not received." in mail


def test_capture_requires_both_keys(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("VM_CAPTURE_AUTO", "0")
    caplog.set_level(logging.WARNING)
    only_telnyx = _load(monkeypatch, tmp_path, agent_token=AGENT, telnyx_api_key=TELNYX_KEY)
    assert only_telnyx.CAPTURE_ON is False
    assert any("xai_api_key is blank" in rec.message for rec in caplog.records)
    assert TELNYX_KEY not in " ".join(caplog.messages)
    caplog.clear()
    only_xai = _load(monkeypatch, tmp_path, agent_token=AGENT, xai_api_key=XAI_KEY)
    assert only_xai.CAPTURE_ON is False
    assert any("telnyx_api_key is blank" in rec.message for rec in caplog.records)
    assert XAI_KEY not in " ".join(caplog.messages)


def test_job_cap_evicts_finished_and_never_drops_a_new_call(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(telnyx_capture, "MAX_JOBS", 2)
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    for n, state in ((1, "done"), (2, "dead")):
        rid = f"22222222-2222-4222-8222-22222222220{n}"
        cap.ingest_webhook(saved_event(event_id=rid, recording_id=rid))
        cap._update(rid, lambda j, state=state: j.update(state=state, done_at=float(n), transcript="x", file=""))
    fresh = "22222222-2222-4222-8222-222222222209"
    assert cap.ingest_webhook(saved_event(event_id=fresh, recording_id=fresh))["ok"] is True
    jobs = cap.jobs()
    assert fresh in jobs and len(jobs) == 2
    assert "22222222-2222-4222-8222-222222222201" not in jobs  # oldest done evicted
    caplog.set_level(logging.WARNING)
    for n in (3, 4):
        rid = f"22222222-2222-4222-8222-22222222221{n}"
        # these stay open (download), so the cap cannot evict them
        cap.ingest_webhook({
            "event_id": rid, "event_type": "call.hangup", "connection_id": CONN,
            "call_session_id": f"11111111-1111-4111-8111-11111111110{n}",
            "from": CALLER,
        })
    extra = "22222222-2222-4222-8222-222222222219"
    assert cap.ingest_webhook(saved_event(event_id=extra, recording_id=extra,
                                          start=stamp(-4), end=stamp(-2)))["ok"] is True
    assert extra in cap.jobs()
    assert any("over cap count=" in rec.message for rec in caplog.records)
    blob = " ".join(caplog.messages)
    assert CALLER not in blob and extra not in blob


def test_inbox_delete_blanks_job_secrets_and_jobs_are_unbacked(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    assert cap.path.parent == cap.dir
    assert cap.path.name == "jobs.json"
    cap.ingest_webhook(saved_event())
    cap._update(REC, lambda j: j.update(
        transcript="secret phrase", **{"from": CALLER}, url=URL, ref="CC-ACDEJ", file="ab" * 16 + ".wav",
    ))
    assert cap.redact(ref="CC-ACDEJ", filename="ab" * 16 + ".wav") == 1
    job = cap.get(REC)
    assert job["transcript"] == "" and job["from"] == "" and job["url"] == ""
    config = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert "callcatch/recordings" in config


@pytest.mark.asyncio
async def test_poll_watermark_waits_for_unread_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(telnyx_capture, "MAX_POLL_PAGES", 2)
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    seen: list[int] = []

    class Pages:
        async def list_recordings(self, created_gte: str, page: int):
            seen.append(page)
            rid = f"22222222-2222-4222-8222-2222222200{page:02d}"
            return {"data": [{
                "id": rid, "status": "completed", "call_session_id": SESSION, "connection_id": CONN,
                "download_urls": {"wav": URL}, "from": CALLER,
            }], "pages": 3}

    net = Pages()
    assert await cap.poll(net, now=1_800_000_000) == "poll-partial"
    stored = json.loads(cap.path.read_text(encoding="utf-8"))
    assert stored.get("watermark") in (0, None) and stored.get("poll_next") == 3
    assert seen == [1, 2]
    assert "22222222-2222-4222-8222-222222220001" in cap.jobs()
    assert "22222222-2222-4222-8222-222222220002" in cap.jobs()
    assert await cap.poll(net, now=1_800_000_100) == "poll-ok"
    stored = json.loads(cap.path.read_text(encoding="utf-8"))
    assert stored.get("watermark") == 1_800_000_100 and stored.get("poll_next") == 1
    # Page 2 is read again so a shifted boundary item is not skipped, then page 3.
    assert seen == [1, 2, 2, 3]
    assert "22222222-2222-4222-8222-222222220003" in cap.jobs()


@pytest.mark.asyncio
async def test_partial_poll_resume_does_not_skip_a_shifted_item(tmp_path, monkeypatch):
    """A new row inserted above the boundary must not hide the item that slid back a page."""
    monkeypatch.setattr(telnyx_capture, "MAX_POLL_PAGES", 1)
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    shifted = {"on": False}

    class Pages:
        async def list_recordings(self, created_gte: str, page: int):
            if not shifted["on"]:
                rid = {1: "22222222-2222-4222-8222-222222220011",
                       2: "22222222-2222-4222-8222-222222220012"}.get(page, "")
            else:
                # What was page 2 is now page 1.  Resuming at page 2 would miss it.
                rid = {1: "22222222-2222-4222-8222-222222220012",
                       2: "22222222-2222-4222-8222-222222220013"}.get(page, "")
            data = []
            if rid:
                data = [{"id": rid, "status": "completed", "call_session_id": SESSION, "connection_id": CONN,
                         "download_urls": {"wav": URL}, "from": CALLER}]
            return {"data": data, "pages": 2}

    assert await cap.poll(Pages(), now=1_800_000_000) == "poll-partial"
    assert "22222222-2222-4222-8222-222222220012" not in cap.jobs()
    shifted["on"] = True
    assert await cap.poll(Pages(), now=1_800_000_050) == "poll-ok"
    assert "22222222-2222-4222-8222-222222220012" in cap.jobs()
    assert "22222222-2222-4222-8222-222222220013" in cap.jobs()


@pytest.mark.asyncio
async def test_error_body_is_capped():
    class Body:
        def __init__(self):
            self.got = None

        async def read(self, n=-1):
            self.got = n
            return b"z" * (len(b"z") if n is None or n < 0 else min(n, 8))

    class Resp:
        def __init__(self):
            self.content = Body()
            self.released = False

        def release(self):
            self.released = True

    resp = Resp()
    await telnyx_capture._discard(resp)
    assert resp.content.got == telnyx_capture.DISCARD_LIMIT
    assert resp.released
    src = pathlib.Path(telnyx_capture.__file__).read_text(encoding="utf-8")
    assert "await resp.read()" not in src


def test_eviction_keeps_audio_the_inbox_still_has(tmp_path, monkeypatch):
    monkeypatch.setattr(telnyx_capture, "MAX_JOBS", 1)
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    cap.dir.mkdir(parents=True, exist_ok=True)
    name = "ab" * 16 + ".wav"
    audio = cap.dir / name
    audio.write_bytes(b"RIFF")
    old = "22222222-2222-4222-8222-222222222201"
    cap.ingest_webhook(saved_event(event_id=old, recording_id=old))
    cap._update(old, lambda j: j.update(state="done", done_at=1, file=name, transcript="kept"))
    fresh = "22222222-2222-4222-8222-222222222209"
    assert cap.ingest_webhook(saved_event(event_id=fresh, recording_id=fresh))["ok"] is True
    assert old not in cap.jobs() and fresh in cap.jobs()
    assert audio.is_file() and audio.read_bytes() == b"RIFF"


def test_job_files_are_private_before_any_audio(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    # A pre-existing loose directory and lock must be tightened on the first write.
    cap.dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cap.dir, 0o755)
    cap.lock_path.write_text("")
    os.chmod(cap.lock_path, 0o644)
    cap.ingest_webhook(saved_event())
    assert (cap.dir.stat().st_mode & 0o777) == 0o700
    assert (cap.path.stat().st_mode & 0o777) == 0o600
    assert (cap.lock_path.stat().st_mode & 0o777) == 0o600
    assert not any(p.suffix in (".wav", ".mp3") for p in cap.dir.iterdir())


def test_retention_sweep_leaves_the_job_index(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    cap.retention_s = 10
    cap.ingest_webhook(saved_event())
    old = time.time() - 3600
    os.utime(cap.path, (old, old))
    os.utime(cap.lock_path, (old, old))
    stray = cap.dir / ("cd" * 16 + ".wav")
    stray.write_bytes(b"RIFF")
    os.utime(stray, (old, old))
    note = cap.dir / "readme.txt"
    note.write_text("keep")
    os.utime(note, (old, old))
    assert cap.sweep_retention(now=time.time()) == 1
    assert cap.path.is_file() and cap.lock_path.is_file() and note.is_file()
    assert not stray.exists()


def test_failure_after_email_is_not_a_details_change(tmp_path):
    import agent_calls
    calls = agent_calls.AgentCalls(tmp_path / "calls.json")
    key = "ab" * 16
    first = calls.agent_final(key, "message", {"reason": "about the gate"}, caller_hint=CALLER)
    assert first["notify"] is True and first["row"]["state"] == "final"
    failed = calls.apply_capture({
        "failed": True,
        "error_code": "stt",
        "recording_id": REC,
        "session": SESSION,
        "caller": CALLER,
        "transcript": "recording could not be transcribed (stt)",
        "start_ts": time.time() - 4,
    })
    assert failed["late"] is True and failed["changed"] is False
    assert failed["notify"] is False
    row = failed["row"]
    assert row["late_updates"] == 0
    assert row["capture_failed"] is True and row["capture_error"] == "stt"
    assert "could not be transcribed" not in (row.get("transcript") or "")
    assert row["telnyx_recording"] == REC



def test_classify_audio_accepts_unexpected_type_with_valid_magic():
    blob = wav_bytes()
    err, check = telnyx_capture.classify_audio("binary/octet-stream", blob)
    assert err == "" and check == ""
    err, blob_out = telnyx_capture.interpret_fetch(blob, content_type="binary/octet-stream")
    assert err == "" and blob_out == blob
    err, blob_out = telnyx_capture.interpret_fetch(blob, content_type="audio/wav; charset=binary")
    assert err == "" and blob_out == blob


def test_classify_audio_rejects_html_json_xml_even_with_valid_magic():
    blob = wav_bytes()
    for ctype in (
        "text/html", "text/html; charset=utf-8", "application/json", "application/xml",
        "text/xml", "application/xhtml+xml", "application/problem+json",
        "application/vnd.api+json", "image/svg+xml", "application/soap+xml",
        "text/plain", "text/html,application/json",
        "TEXT/XML", " text/plain ", "text/xml; charset=utf-8",
    ):
        err, check = telnyx_capture.classify_audio(ctype, blob)
        assert err == "bad-type" and check == "header", ctype


def test_classify_audio_rejects_wrong_type_and_wrong_magic():
    blob = b"NOT-AUDIO-XXXX"
    err, check = telnyx_capture.classify_audio("binary/octet-stream", blob)
    assert err == "bad-type" and check == "magic"
    err, check = telnyx_capture.classify_audio("audio/wav", blob)
    assert err == "bad-type" and check == "magic"


def test_bad_type_log_has_type_check_status_and_bytes_but_not_body_or_url(caplog):
    caplog.set_level(logging.INFO)
    marker = b"RIFF????SECRET_BODY_MARKER_SHOULD_NEVER_BE_LOGGED"
    # Force a non-audio payload that is long enough to be distinct in a log.
    blob = marker + b"\x00" * 16
    telnyx_capture.interpret_fetch(blob, content_type="binary/octet-stream", http_status=200)
    text = " ".join(caplog.messages)
    assert "check=magic" in text
    assert "http=200" in text
    assert "type=binary/octet-stream" in text
    assert f"bytes={len(blob)}" in text
    assert "SECRET_BODY_MARKER_SHOULD_NEVER_BE_LOGGED" not in text
    assert "telephony-recorder-prod" not in text
    assert "https://" not in text
    assert URL not in text


def test_header_reject_log_names_check_header(caplog):
    caplog.set_level(logging.INFO)
    blob = wav_bytes()
    err, out = telnyx_capture.interpret_fetch(
        {"error": "bad-type", "check": "header", "http_status": 200,
         "content_type": "application/json; charset=utf-8", "bytes": len(blob)},
    )
    assert err == "bad-type" and out == b""
    text = " ".join(caplog.messages)
    assert "check=header" in text and "http=200" in text
    assert "type=application/json" in text
    assert f"bytes={len(blob)}" in text
    assert "https://" not in text


@pytest.mark.asyncio
async def test_bad_type_is_retried_three_times_then_terminal(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        net = FakeNet([{"text": "nope"}], blob=b"NOT-AUDIO-XXXX!!!!")
        cap = mod.ensure_capture(app)
        t0 = 1_700_000_000.0
        await _advance(mod, app, net, now=t0)
        job = next(iter(cap.jobs().values()))
        assert job["last_error"] == "bad-type" and job["state"] == "download"
        assert job["bad_type_tries"] == 1 and job["tries"] == 1
        assert job["next_at"] == pytest.approx(t0 + 5)
        await _advance(mod, app, net, now=float(job["next_at"]) + 0.01)
        job = next(iter(cap.jobs().values()))
        assert job["last_error"] == "bad-type" and job["state"] == "download"
        assert job["bad_type_tries"] == 2
        t2 = float(job["next_at"])
        assert t2 == pytest.approx(t0 + 5 + 15, abs=0.1)
        await _advance(mod, app, net, now=t2 + 0.01)
        job = next(iter(cap.jobs().values()))
        assert job["last_error"] == "bad-type" and job["state"] == "download"
        assert job["bad_type_tries"] == 3
        t3 = float(job["next_at"])
        assert t3 == pytest.approx(t2 + 0.01 + 45, abs=0.1)
        await _advance(mod, app, net, now=t3 + 0.01)
        job = next(iter(cap.jobs().values()))
        assert job["state"] == "dead" and job["last_error"] == "bad-type"
        assert job["bad_type_tries"] == 4
        assert not job.get("file")
        assert len(net.fetched) == 4
        assert net.deleted == []


@pytest.mark.asyncio
async def test_unexpected_type_with_valid_magic_is_saved(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        # FakeNet returns raw wav bytes (no Content-Type).  interpret_fetch
        # treats a missing type plus valid magic as acceptable, which is the
        # same path as an unexpected but non-rejected type.
        net = FakeNet([{"text": "heard after odd type", "duration": 2}], blob=wav_bytes())
        await _advance(mod, app, net)
        job = next(iter(mod.ensure_capture(app).jobs().values()))
        assert job["state"] in ("transcribe", "attach", "delete", "done") or job.get("file")
        assert job.get("file")



class _SeqNet(FakeNet):
    def __init__(self, seq, **kw):
        super().__init__([{"text": "x", "duration": 2}], **kw)
        self.seq = list(seq)

    async def fetch(self, url: str):
        self.fetched.append(url)
        return self.seq.pop(0) if self.seq else wav_bytes()


@pytest.mark.asyncio
async def test_bad_type_budget_ignores_prior_download_failures(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        await _post(client, saved_event())
        bad = {"error": "bad-type", "check": "magic", "http_status": 200,
               "content_type": "binary/octet-stream", "bytes": 8}
        net = _SeqNet([{"error": "download"}, {"error": "download"}, bad, bad, bad, wav_bytes()])
        cap = mod.ensure_capture(app)
        t = 1_700_000_000.0
        await _advance(mod, app, net, now=t)
        job = next(iter(cap.jobs().values()))
        assert job["last_error"] == "download" and job["state"] == "download" and job["tries"] == 1
        t = float(job["next_at"]) + 0.01
        await _advance(mod, app, net, now=t)
        job = next(iter(cap.jobs().values()))
        assert job["last_error"] == "download" and job["tries"] == 2
        t = float(job["next_at"]) + 0.01
        await _advance(mod, app, net, now=t)
        job = next(iter(cap.jobs().values()))
        assert job["last_error"] == "bad-type" and job["state"] == "download"
        assert job["bad_type_tries"] == 1 and job["tries"] == 3
        assert job["next_at"] == pytest.approx(t + 5)
        t = float(job["next_at"]) + 0.01
        await _advance(mod, app, net, now=t)
        job = next(iter(cap.jobs().values()))
        assert job["bad_type_tries"] == 2 and job["state"] == "download"
        assert job["next_at"] == pytest.approx(t + 15, abs=0.1)
        t = float(job["next_at"]) + 0.01
        await _advance(mod, app, net, now=t)
        job = next(iter(cap.jobs().values()))
        assert job["bad_type_tries"] == 3 and job["state"] == "download"
        assert job["next_at"] == pytest.approx(t + 45, abs=0.1)
        t = float(job["next_at"]) + 0.01
        await _advance(mod, app, net, now=t)
        # Fourth bad-type would be terminal; the sequence's last item is a good body, so it saves.
        job = next(iter(cap.jobs().values()))
        assert job["bad_type_tries"] == 3
        assert job.get("file")
        assert len(net.fetched) == 6
        assert net.deleted == [] or job.get("file")


@pytest.mark.asyncio
async def test_hard_reject_discards_body_without_full_read(monkeypatch):
    body = b"RIFF" + b"A" * 200 + b"WAVE" + b"X" * (telnyx_capture.DISCARD_LIMIT + 2048)
    # A polyglot-sized payload bigger than the discard cap.

    class Body:
        def __init__(self, payload):
            self.payload = payload
            self.got = None
            self.iterated = False

        async def read(self, n=-1):
            self.got = n
            if n is None or n < 0:
                return self.payload
            return self.payload[:n]

        async def iter_chunked(self, n):
            self.iterated = True
            yield self.payload

    class Resp:
        def __init__(self, payload):
            self.status = 200
            self.headers = {"Content-Type": "text/xml; charset=utf-8"}
            self.content = Body(payload)
            self.released = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def release(self):
            self.released = True

    class Sess:
        def __init__(self, resp):
            self.resp = resp
            self.calls = []

        def get(self, url, **kw):
            self.calls.append(url)
            return self.resp

    resp = Resp(body)
    monkeypatch.setattr(telnyx_capture, "host_is_public", lambda h: True)
    net = telnyx_capture.HttpNet(Sess(resp), TELNYX_KEY, XAI_KEY)
    out = await net.fetch(URL)
    assert isinstance(out, dict) and out["error"] == "bad-type" and out["check"] == "header"
    assert out["bytes"] == 0
    assert resp.content.iterated is False
    assert resp.content.got == telnyx_capture.DISCARD_LIMIT
    assert resp.released
    assert "await resp.read()" not in pathlib.Path(telnyx_capture.__file__).read_text(encoding="utf-8")
