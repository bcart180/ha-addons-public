"""CallCatch Voice smoke tests. Run: python3 twilio_house_voice/tests/test_voice.py"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import pathlib
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.parse

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP))

import audio  # noqa: E402
import server  # noqa: E402

CALL = "CA1234567890abcdef"
SILENCE = base64.b64encode(bytes([0xFF]) * 160).decode()
LOUD = base64.b64encode(bytes([0x00]) * 80).decode()
XAI_KEY = "xai-test-key-should-not-leak"
AUTH = "twilio-auth-token-value"


def independent_signature(token, url, params):
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def stream_path(xml):
    """Return (websocket path, one-time token). The Stream url has no query."""
    match = re.search(r'<Stream url="([^"]+)"', xml)
    assert match, xml
    url = html.unescape(match.group(1))
    parsed = urllib.parse.urlparse(url)
    assert parsed.query == "", url
    assert parsed.fragment == "", url
    assert parsed.path == "/twilio/stream", url
    token_m = re.search(r'<Parameter name="token" value="([^"]*)"', xml)
    call_m = re.search(r'<Parameter name="call_sid" value="([^"]*)"', xml)
    assert token_m and call_m, xml
    return parsed.path, html.unescape(token_m.group(1))


def media_start(stream_sid, token, call_id=CALL):
    return {
        "event": "start",
        "start": {
            "streamSid": stream_sid,
            "callSid": call_id,
            "tracks": ["inbound"],
            "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
            "customParameters": {"token": token, "call_sid": call_id},
        },
    }


async def expect_policy_close(ws, timeout=2):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    assert msg.type in (
        aiohttp.WSMsgType.CLOSE,
        aiohttp.WSMsgType.CLOSING,
        aiohttp.WSMsgType.CLOSED,
    ), msg
    if msg.type == aiohttp.WSMsgType.CLOSE and msg.data is not None:
        assert int(msg.data) == server.STREAM_REJECT_CODE, msg.data
    return msg


async def wait_for(pred, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_mulaw_wav_silence_and_peak():
    assert audio.mulaw_byte_to_pcm(0xFF) == 0
    assert audio.mulaw_byte_to_pcm(0x00) == -32124
    wav = audio.mulaw_wav(bytes([0xFF]) * 160)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    fmt, channels, rate = struct.unpack_from("<HHI", wav, 20)
    assert (fmt, channels, rate) == (1, 1, 8000)
    assert wav[34:36] == struct.pack("<H", 16)
    data = wav[44:]
    assert len(data) == 320
    assert data == b"\x00" * 320
    peak = audio.mulaw_wav(bytes([0x00]))
    sample = struct.unpack_from("<h", peak, 44)[0]
    assert sample == -32124


def test_mulaw_roundtrip_mix_and_timeline():
    for byte in range(256):
        pcm = audio.mulaw_byte_to_pcm(byte)
        assert audio.mulaw_byte_to_pcm(audio.pcm16_to_mulaw(pcm)) == pcm
    # 0x7F and 0xFF both decode to 0. Encoding zero uses the canonical silence byte.
    assert audio.pcm16_to_mulaw(0) == audio.MULAW_SILENCE
    assert audio.mulaw_byte_to_pcm(0x7F) == 0
    assert audio.mix_mulaw(b"", b"") == b""
    caller = bytes([0x00]) * 80 + bytes([0xFF]) * 80
    assistant = bytes([0xFF]) * 160 + bytes([0x00]) * 40
    mixed = audio.mix_mulaw(caller, assistant)
    assert mixed == caller + bytes([0x00]) * 40
    both = audio.mix_mulaw(bytes([0x00]), bytes([0x00]))
    assert both == bytes([audio.pcm16_to_mulaw(-32768)])
    tl = audio.MulawTimeline()
    assert tl.place(0, bytes([0x00]) * 160) == 160
    assert tl.place(160, bytes([0xFF]) * 160) == 160
    assert len(tl.segments) == 1
    assert tl.place(800, bytes([0x00]) * 100) == 100
    assert tl.truncate(840) == 60
    assert tl.voiced == 360
    rendered = tl.render()
    assert len(rendered) == 840
    assert rendered[:160] == bytes([0x00]) * 160
    assert rendered[200] == 0xFF
    assert rendered[400] == 0xFF
    assert rendered[800:840] == bytes([0x00]) * 40
    small = audio.MulawTimeline(max_bytes=100)
    assert small.place(50, bytes([0x00]) * 80) == 50
    assert small.truncated
    assert small.render() == bytes([0xFF]) * 50 + bytes([0x00]) * 50


def _pcm(wav: bytes):
    assert wav[:4] == b"RIFF"
    return wav[44:]


def test_persist_mix_aligns_clear_and_falls_back():
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app({
            "xai_bridge": "stub",
            "record_caller_audio": True,
            "record_assistant_audio": True,
            "mix_recording": True,
            "log_level": "error",
        }, data_dir=tmp)
        session = server.CallSession(app, CALL, {"from": "+12025550101", "to": "+12025550111"})
        clock = {"t": 0.0}
        session.clock = lambda: clock["t"]
        session.stream_mono = 0.0
        session.record_caller(bytes([0x00]) * 160, 0)
        session.record_caller(bytes([0xFF]) * 160, 20)
        clock["t"] = 0.125
        session.record_assistant(bytes([0x00]) * 320)
        clock["t"] = 0.15625
        session.barge_in()
        start = audio.ms_to_sample(125)
        cut = audio.ms_to_sample(156.25)
        assert session.assistant_tl.voiced == cut - start
        assert 0 < session.assistant_tl.voiced < 320
        record = await server.persist_call(session)
        assert record["audio_mix"] is True
        assert record["assistant_audio_bytes"] == cut - start
        pcm = _pcm(pathlib.Path(record["audio_path"]).read_bytes())
        assert len(pcm) == cut * 2
        samples = struct.unpack("<" + "h" * cut, pcm)
        assert samples[0] == -32124
        assert samples[200] == 0
        assert samples[start - 1] == 0
        assert samples[start] == -32124
        assert samples[cut - 1] == -32124
        assistant_pcm = _pcm((pathlib.Path(tmp) / "voicemail" / f"{CALL}.assistant.wav").read_bytes())
        asamples = struct.unpack("<" + "h" * cut, assistant_pcm)
        assert asamples[0] == 0 and asamples[start] == -32124
        side = json.loads((pathlib.Path(tmp) / "voicemail" / f"{CALL}.json").read_text())
        assert side["audio_mix"] is True
        assert "_caller_wav" not in side

        plain = server.CallSession(app, "CA1234567890plain1", {"from": "+12025550101"})
        plain.record_caller(bytes([0x00]) * 80, None)
        plain.record_caller(bytes([0xFF]) * 160, None)
        plain.record_caller(bytes([0x00]) * 80, 120_000)
        assert plain.timestamp_fallback is True
        assert len(plain.caller_tl.render()) == 320
        caller_only = await server.persist_call(plain)
        assert caller_only["audio_mix"] is False
        assert _pcm(pathlib.Path(caller_only["audio_path"]).read_bytes()) == audio.mulaw_to_pcm16(
            bytes([0x00]) * 80 + bytes([0xFF]) * 160 + bytes([0x00]) * 80
        )

        off = server.create_app({
            "xai_bridge": "stub",
            "record_caller_audio": True,
            "record_assistant_audio": True,
            "mix_recording": False,
            "log_level": "error",
        }, data_dir=tmp)
        separate = server.CallSession(off, "CA1234567890separ1", {"from": "+12025550101"})
        separate.record_caller(bytes([0x00]) * 80, None)
        separate.record_assistant(bytes([0x7F]) * 40)
        separate_rec = await server.persist_call(separate)
        assert separate_rec["audio_mix"] is False
        assert _pcm(pathlib.Path(separate_rec["audio_path"]).read_bytes()) == audio.mulaw_to_pcm16(bytes([0x00]) * 80)
        assert (pathlib.Path(tmp) / "voicemail" / "CA1234567890separ1.assistant.wav").is_file()

        no_caller = server.create_app({
            "xai_bridge": "stub",
            "record_caller_audio": False,
            "mix_recording": True,
            "log_level": "error",
        }, data_dir=tmp)
        silent_main = server.CallSession(no_caller, "CA1234567890nocall", {"from": "+12025550101"})
        silent_main.record_assistant(bytes([0x00]) * 80)
        missing = await server.persist_call(silent_main)
        assert missing["audio_path"] is None
        assert missing["audio_mix"] is False

        greeting = server.CallSession(app, "CA1234567890greet1", {"from": "+12025550101"})
        greeting.clock = lambda: 0.0
        greeting.stream_mono = 0.0
        greeting.record_assistant(bytes([0x00]) * 80)
        heard = await server.persist_call(greeting)
        assert heard["audio_mix"] is True
        assert _pcm(pathlib.Path(heard["audio_path"]).read_bytes()) == audio.mulaw_to_pcm16(bytes([0x00]) * 80)

    asyncio.run(go())


def test_replacement_stream_offsets_caller_timestamps():
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app({"xai_bridge": "stub", "log_level": "error"}, data_dir=tmp)
        session = server.CallSession(app, CALL, {"from": "+12025550101"})
        await session.on_twilio(media_start("MZ11111111111111", "tok"))
        await session.on_twilio({
            "event": "media",
            "media": {"track": "inbound", "timestamp": "0", "payload": SILENCE},
        })
        await session.on_twilio(media_start("MZ22222222222222", "tok"))
        await session.on_twilio({
            "event": "media",
            "media": {"track": "inbound", "timestamp": "0", "payload": LOUD},
        })
        assert session.caller_tl.render() == bytes([0xFF]) * 160 + bytes([0x00]) * 80

    asyncio.run(go())


def test_session_update_is_pcmu_passthrough():
    opts = server.Opts({"xai_voice": "eve", "xai_instructions": "Be brief."})
    msg = server.session_update_message(opts, "Be brief.")
    audio_cfg = msg["session"]["audio"]
    assert audio_cfg["input"]["format"]["type"] == "audio/pcmu"
    assert audio_cfg["output"]["format"]["type"] == "audio/pcmu"
    assert "rate" not in audio_cfg["input"]["format"]
    assert msg["session"]["turn_detection"]["type"] == "server_vad"
    assert msg["session"]["voice"] == "eve"


def test_twiml_live_and_stub():
    live = server.build_twiml("https://voice.example", "tok_en", CALL, speak_fallback=False)
    assert live.startswith("<?xml")
    assert "<Connect " in live
    assert 'url="wss://voice.example/twilio/stream"' in live
    assert "?token=" not in live
    assert "?" not in re.search(r'<Stream url="[^"]*"', live).group(0)
    assert '<Parameter name="token" value="tok_en"/>' in live
    assert f'<Parameter name="call_sid" value="{CALL}"/>' in live
    assert "<Say>" not in live
    assert XAI_KEY not in live
    stub = server.build_twiml("http://127.0.0.1:8787", "abc", CALL, speak_fallback=True)
    assert "<Say>" in stub
    assert 'url="ws://127.0.0.1:8787/twilio/stream"' in stub
    assert "?token=" not in stub
    assert '<Parameter name="token" value="abc"/>' in stub
    odd = server.build_twiml("https://voice.example", 'a&b"c', CALL, speak_fallback=False)
    assert 'url="wss://voice.example/twilio/stream"' in odd
    assert 'name="token" value="a&amp;b&quot;c"' in odd


def test_signature_and_ingress_rules():
    params = {"CallSid": CALL, "From": "+12025550101"}
    url = "https://voice.example/twilio/voice"
    good = independent_signature(AUTH, url, params)
    assert server.signature_ok(AUTH, url, params, good)
    assert not server.signature_ok(AUTH, url, params, "nope")
    opts = server.Opts({"twilio_auth_token": AUTH, "validate_twilio_signature": True})
    assert server.twilio_authorized(opts, "8.8.8.8", url, params, good)
    assert not server.twilio_authorized(opts, "127.0.0.1", url, params, "bad")
    open_opts = server.Opts({})
    assert not server.twilio_authorized(open_opts, "127.0.0.1", url, params, "")
    assert not server.twilio_authorized(open_opts, "8.8.8.8", url, params, "")
    assert not server.ingress_blocked("/twilio/voice", "8.8.8.8", True)
    assert not server.ingress_blocked("/api/health", "8.8.8.8", True)
    assert server.ingress_blocked("/api/status", "8.8.8.8", True)
    assert not server.ingress_blocked("/", "172.30.32.2", True)
    assert not server.ingress_blocked("/api/status", "8.8.8.8", False)


def test_instructions_path_and_redact_and_b64_cap():
    assert not server.path_under_allowed(server.resolve_instructions_path("/etc/passwd"))
    assert not server.path_under_allowed(server.resolve_instructions_path("../../etc/passwd"))
    assert server.path_under_allowed(server.resolve_instructions_path("/data/voicemail/instructions.txt"))
    assert server.path_under_allowed(server.resolve_instructions_path("house.txt"))
    server.setup_logging("INFO", [XAI_KEY, AUTH])
    hidden = server.redact(f"Authorization: Bearer {XAI_KEY} token {AUTH}")
    assert XAI_KEY not in hidden and AUTH not in hidden and "***" in hidden
    encoded, why = server.maybe_audio_b64(b"wav", True)
    assert encoded and why is None
    _none, why = server.maybe_audio_b64(b"x" * (server.AUDIO_B64_LIMIT + 1), True)
    assert why == "too_large"
    same = server.webhook_targets(server.Opts({
        "ha_webhook_transcript": "https://ha.example/hook",
        "ha_webhook_audio": "https://ha.example/hook",
    }))
    assert len(same) == 1 and same[0][2] is True


def test_config_version_and_readme():
    config = (ROOT / "twilio_house_voice" / "config.yaml").read_text()
    assert re.search(r'(?m)^version:\s*"0\.3\.2"\s*$', config)
    assert re.search(r"(?m)^slug:\s*twilio_house_voice\s*$", config)
    assert "8787" in config
    assert server.VERSION == "0.3.2"
    readme = (ROOT / "README.md").read_text()
    addon = (ROOT / "twilio_house_voice" / "README.md").read_text()
    assert "twilio_house_voice" in readme
    assert "Check for updates" in addon
    assert "mix_recording" in addon
    assert "https://github.com/bcart180/ha-addons-public" in addon
    docs = (ROOT / "twilio_house_voice" / "DOCS.md").read_text()
    assert "mix_recording" in docs
    assert "separate files" not in docs
    assert server.Opts({}).mix_recording is True
    assert server.Opts({"mix_recording": "off"}).mix_recording is False
    assert XAI_KEY not in addon and "AC" + "000" not in config
    repo = (ROOT / "repository.yaml").read_text()
    assert "twilio_house_voice" in repo


def _opts(tmp, **extra):
    base = {
        "xai_bridge": "stub",
        "public_base_url": "https://voice.example",
        "twilio_auth_token": AUTH,
        "record_caller_audio": True,
        "greeting_mode": "agent",
        "log_level": "warning",
    }
    base.update(extra)
    return base


async def _post_voice(client, call_id=CALL, headers=None, **fields):
    data = {"CallSid": call_id, "From": "+12025550101", "To": "+12025550143", "Direction": "inbound"}
    data.update(fields)
    hdrs = _sig("/twilio/voice", data)
    hdrs.update(headers or {})
    return await client.post("/twilio/voice", data=data, headers=hdrs)


def _signed_post(client, path, data):
    return client.post(path, data=data, headers=_sig(path, data))


def _sig(path, data):
    """X-Twilio-Signature for a test POST, as Twilio would sign it."""
    return {"X-Twilio-Signature": independent_signature(AUTH, "https://voice.example" + path, data)}


def test_stub_hangup_writes_wav_and_webhook_is_idempotent():
    async def go():
        hooks = []

        async def hook(request):
            hooks.append({
                "body": await request.json(),
                "key": request.headers.get("X-Idempotency-Key"),
                "auth": request.headers.get("Authorization"),
            })
            return web.json_response({"ok": True})

        hook_app = web.Application()
        hook_app.router.add_post("/hook", hook)
        hook_server = TestServer(hook_app)
        await hook_server.start_server()
        tmp = tempfile.mkdtemp()
        try:
            hook_url = str(hook_server.make_url("/hook"))
            app = server.create_app(_opts(
                tmp,
                ha_webhook_transcript=hook_url,
                ha_webhook_token="hook-token-secret",
                include_audio_base64=True,
                xai_api_key=XAI_KEY,
                xai_bridge="stub",
            ), data_dir=tmp)
            async with TestClient(TestServer(app)) as client:
                got = await _post_voice(client)
                assert got.status == 200, await got.text()
                xml = await got.text()
                assert "<Say>" in xml
                assert XAI_KEY not in xml
                path, token = stream_path(xml)
                ws = await client.ws_connect(path)
                await ws.send_json({"event": "connected", "protocol": "Call"})
                await ws.send_json(media_start("MZ1234567890abcd", token))
                await ws.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
                await ws.send_json({"event": "stop"})
                assert await wait_for(lambda: len(hooks) == 1)
                await ws.close()
                body = hooks[0]["body"]
                assert body["call_id"] == CALL
                assert hooks[0]["key"] == CALL
                assert hooks[0]["auth"] == "Bearer hook-token-secret"
                assert body["token"] == "hook-token-secret"
                assert body["from"] == "+12025550101"
                assert body["audio_path"].endswith(f"{CALL}.wav")
                assert body["audio_base64"]
                wav = pathlib.Path(tmp) / "voicemail" / f"{CALL}.wav"
                assert wav.is_file()
                assert wav.read_bytes()[:4] == b"RIFF"
                side = json.loads((pathlib.Path(tmp) / "voicemail" / f"{CALL}.json").read_text())
                assert "audio_base64" not in side
                assert "token" not in side
                assert "hook-token-secret" not in json.dumps(side)
                assert XAI_KEY not in json.dumps(side)
                assert side["ha_post"]["transcript"]["ok"] is True
                health = await client.get("/api/health")
                assert await health.json() == {"ok": True}
                status = await client.get("/api/status")
                payload = await status.json()
                assert payload["version"] == "0.3.2"
                assert payload["mix_recording"] is True
                assert payload["bridge"] == "stub"
                dumped = json.dumps(payload)
                assert XAI_KEY not in dumped
                assert "hook-token-secret" not in dumped
                assert "+12025550101" not in dumped
                assert payload["recent"][0]["from"].endswith("0101")
                again = await _signed_post(client, "/twilio/status", {
                    "CallSid": CALL, "CallStatus": "completed", "From": "+12025550101",
                })
                assert again.status == 200
                await asyncio.sleep(0.3)
                assert len(hooks) == 1
                missing = await _signed_post(client, "/twilio/voice", {"From": "+1555"})
                assert missing.status == 400
                get_voice = await client.get("/twilio/voice")
                assert get_voice.status == 405
                denied = await client.get("/twilio/stream")
                assert denied.status == 403
        finally:
            await hook_server.close()

    asyncio.run(go())


def test_live_bridge_forwards_pcmu_and_writes_transcript():
    async def go():
        seen = []
        greeted = asyncio.Event()
        appended = asyncio.Event()
        auth_ok = {"value": False}

        async def fake_xai(request):
            auth_ok["value"] = request.headers.get("Authorization") == f"Bearer {XAI_KEY}"
            assert XAI_KEY not in str(request.url)
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                seen.append(data.get("type"))
                if data.get("type") == "session.update":
                    fmt = data["session"]["audio"]
                    assert fmt["input"]["format"]["type"] == "audio/pcmu"
                    assert fmt["output"]["format"]["type"] == "audio/pcmu"
                    await ws.send_json({"type": "session.updated"})
                elif data.get("type") == "response.create":
                    greeted.set()
                elif data.get("type") == "input_audio_buffer.append":
                    assert data["audio"] == SILENCE
                    appended.set()
                    await ws.send_json({"type": "response.created", "response": {"id": "r1"}})
                    await ws.send_json({
                        "type": "response.output_audio.delta",
                        "delta": SILENCE,
                        "response_id": "r1",
                    })
                    await ws.send_json({
                        "type": "response.output_audio_transcript.done",
                        "transcript": "Hello from the house",
                    })
                    await ws.send_json({
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "Hi this is Sam",
                    })
                    # 20 ms of assistant audio must count as played before clear.
                    await asyncio.sleep(0.05)
                    await ws.send_json({"type": "input_audio_buffer.speech_started"})
            return ws

        hooks = []

        async def hook(request):
            hooks.append(await request.json())
            return web.json_response({"ok": True})

        fake_app = web.Application()
        fake_app.router.add_get("/v1/realtime", fake_xai)
        fake_app.router.add_post("/hook", hook)
        fake = TestServer(fake_app)
        await fake.start_server()
        tmp = tempfile.mkdtemp()
        try:
            xai_url = f"ws://{fake.host}:{fake.port}/v1/realtime?model=grok-voice-latest"
            app = server.create_app({
                "xai_bridge": "live",
                "xai_api_key": XAI_KEY,
                "xai_voice": "eve",
                "xai_realtime_url": xai_url,
                "public_base_url": "https://voice.example",
                "twilio_auth_token": AUTH,
                "ha_webhook_audio": str(fake.make_url("/hook")),
                "record_caller_audio": True,
                "record_assistant_audio": True,
                "greeting_mode": "agent",
                "include_audio_base64": False,
                "log_level": "warning",
            }, data_dir=tmp)
            async with TestClient(TestServer(app)) as client:
                got = await _post_voice(client)
                xml = await got.text()
                assert "<Say>" not in xml
                assert 'url="wss://voice.example/twilio/stream"' in xml
                assert "?token=" not in xml
                path, token = stream_path(xml)
                ws = await client.ws_connect(path)
                await ws.send_json(media_start("MZ1234567890abcd", token))
                await asyncio.wait_for(greeted.wait(), 3)
                await ws.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
                await asyncio.wait_for(appended.wait(), 3)
                kinds = set()
                deadline = time.time() + 3
                while time.time() < deadline and not ({"media", "clear"} <= kinds):
                    incoming = await asyncio.wait_for(ws.receive(), 3)
                    if incoming.type != aiohttp.WSMsgType.TEXT:
                        continue
                    event = json.loads(incoming.data)
                    kinds.add(event.get("event"))
                    if event.get("event") == "media":
                        assert event["media"]["payload"] == SILENCE
                        assert event["streamSid"] == "MZ1234567890abcd"
                assert {"media", "clear"} <= kinds, kinds
                await ws.send_json({"event": "stop"})
                # speech_started with no transcript yet: finalize drains xAI
                # for up to FINALIZE_DRAIN_S before posting.
                assert await wait_for(lambda: len(hooks) == 1, timeout=server.FINALIZE_DRAIN_S + 3)
                await ws.close()
                body = hooks[0]
                assert body["xai_bridge"] == "live"
                assert body["xai_connected"] is True
                assert "Hello from the house" in body["transcript_text"]
                assert "Hi this is Sam" in body["transcript_text"]
                assert "audio_base64" not in body
                assert "token" not in body
                assert auth_ok["value"] is True
                assert "session.update" in seen and "input_audio_buffer.append" in seen
                caller = pathlib.Path(tmp) / "voicemail" / f"{CALL}.wav"
                assistant = pathlib.Path(tmp) / "voicemail" / f"{CALL}.assistant.wav"
                assert caller.is_file() and assistant.is_file()
                side = json.loads((pathlib.Path(tmp) / "voicemail" / f"{CALL}.json").read_text())
                assert XAI_KEY not in json.dumps(side)
                assert side["audio_mix"] is True
                assert side["assistant_audio_bytes"] > 0
        finally:
            await fake.close()

    asyncio.run(go())


def test_second_stream_keeps_one_session():
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app(_opts(tmp), data_dir=tmp)
        async with TestClient(TestServer(app)) as client:
            first = await _post_voice(client)
            second = await _post_voice(client)
            path1, token1 = stream_path(await first.text())
            path2, token2 = stream_path(await second.text())
            assert path1 == path2 == "/twilio/stream"
            assert token1 != token2
            ws1 = await client.ws_connect(path1)
            await ws1.send_json(media_start("MZ11111111111111", token1))
            await ws1.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
            await asyncio.sleep(0.05)
            ws2 = await client.ws_connect(path2)
            await ws2.send_json(media_start("MZ22222222222222", token2))
            await ws2.send_json({"event": "media", "media": {"track": "inbound", "payload": LOUD}})
            await ws1.close()
            await asyncio.sleep(0.3)
            wav = pathlib.Path(tmp) / "voicemail" / f"{CALL}.wav"
            assert not wav.exists()
            await ws2.send_json({"event": "stop"})
            assert await wait_for(lambda: wav.is_file())
            data = wav.read_bytes()
            assert len(data) == 44 + (160 + 80) * 2
            samples = struct.unpack("<" + "h" * 240, data[44:])
            assert samples[0] == 0
            assert samples[-1] == -32124
            await ws2.close()

    asyncio.run(go())


def test_signature_required_when_token_set():
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app({
            "twilio_auth_token": AUTH,
            "validate_twilio_signature": True,
            "public_base_url": "https://voice.example",
            "xai_bridge": "stub",
            "log_level": "error",
        }, data_dir=tmp)
        params = {"CallSid": CALL, "From": "+12025550101", "To": "+15550000"}
        url = "https://voice.example/twilio/voice"
        good = independent_signature(AUTH, url, params)
        async with TestClient(TestServer(app)) as client:
            bad = await client.post("/twilio/voice", data=params)
            assert bad.status == 403
            ok = await client.post("/twilio/voice", data=params, headers={"X-Twilio-Signature": good})
            assert ok.status == 200
            xml = await ok.text()
            assert AUTH not in xml
            path, token = stream_path(xml)
            assert path == "/twilio/stream"
            assert token
            assert "?token=" not in xml
            assert 'url="wss://voice.example/twilio/stream"' in xml

    asyncio.run(go())


def test_stream_auth_is_start_parameter_not_query():
    """Upgrade first. Token is customParameters on start, never the query string."""
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app(_opts(tmp, log_level="error"), data_dir=tmp)
        previous_wait = server.STREAM_START_WAIT_S
        server.STREAM_START_WAIT_S = 0.4
        try:
            async with TestClient(TestServer(app)) as client:
                plain = await client.get("/twilio/stream")
                assert plain.status == 403

                idle = await client.ws_connect("/twilio/stream")
                await idle.send_json({"event": "connected", "protocol": "Call"})
                await expect_policy_close(idle)
                assert app["sessions"] == {}

                missing = await client.ws_connect("/twilio/stream")
                await missing.send_json({
                    "event": "start",
                    "start": {
                        "streamSid": "MZaaaaaaaaaaaaaa",
                        "callSid": CALL,
                        "customParameters": {},
                    },
                })
                await expect_policy_close(missing)
                assert CALL not in app["sessions"]

                server.STREAM_START_WAIT_S = previous_wait
                got = await _post_voice(client)
                assert got.status == 200
                path, token = stream_path(await got.text())
                assert token not in path
                app["tokens"][token]["exp"] = time.time() - 1
                expired = await client.ws_connect(path)
                await expired.send_json(media_start("MZbbbbbbbbbbbbbb", token))
                await expect_policy_close(expired)
                assert token not in app["tokens"]
                assert CALL not in app["sessions"]

                again = await _post_voice(client)
                path, token = stream_path(await again.text())
                bad = await client.ws_connect(path + "?token=" + urllib.parse.quote(token))
                await bad.send_json({
                    "event": "connected",
                    "protocol": "Call",
                })
                await bad.send_json({
                    "event": "start",
                    "start": {
                        "streamSid": "MZcccccccccccccc",
                        "callSid": CALL,
                        "customParameters": {"call_sid": CALL},
                    },
                })
                await expect_policy_close(bad)
                assert token in app["tokens"]
                assert CALL not in app["sessions"]

                wrong = await client.ws_connect(path)
                await wrong.send_json(media_start("MZdddddddddddddd", "not-the-minted-token"))
                await expect_policy_close(wrong)
                assert token in app["tokens"]

                good = await client.ws_connect(path)
                await good.send_json({"event": "connected", "protocol": "Call"})
                await good.send_json(media_start("MZeeeeeeeeeeeeee", token))
                assert await wait_for(
                    lambda: CALL in app["sessions"] and app["sessions"][CALL].stream_sid
                )
                assert token not in app["tokens"]
                session = app["sessions"][CALL]
                assert session.stream_sid == "MZeeeeeeeeeeeeee"
                assert session.replaced == 0

                replay = await client.ws_connect(path)
                await replay.send_json(media_start("MZffffffffffff", token))
                await expect_policy_close(replay)
                assert app["sessions"][CALL] is session
                assert session.replaced == 0
                assert session.twilio_ws is not None and not session.twilio_ws.closed
                assert not good.closed

                await good.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
                await good.send_json({"event": "stop"})
                wav = pathlib.Path(tmp) / "voicemail" / f"{CALL}.wav"
                assert await wait_for(lambda: wav.is_file())
                await good.close()
        finally:
            server.STREAM_START_WAIT_S = previous_wait

    asyncio.run(go())


def _session(tmp=None):
    tmp = tmp or tempfile.mkdtemp()
    app = server.create_app({"xai_bridge": "stub", "log_level": "error"}, data_dir=tmp)
    return server.CallSession(app, CALL, {"from": "+12025550101"})


def test_speaker_label_renders_assistant_and_caller():
    assert server.speaker_label("caller") == "Caller"
    assert server.speaker_label("Caller") == "Caller"
    assert server.speaker_label("CALLER") == "Caller"
    for role in ("house", "House", "HOUSE", "assistant", "Assistant", "agent", "Agent"):
        assert server.speaker_label(role) == "Assistant", role
    session = _session()
    session.add_turn("house", "Saved as house", final=True)
    session.add_turn("agent", "Saved as agent", final=True)
    session.add_turn("assistant", "Hello from the house", final=True)
    session.add_turn("caller", "Hi this is Sam", final=True)
    assert [t["role"] for t in session.turns] == ["house", "agent", "assistant", "caller"]
    assert session.transcript_text() == (
        "Assistant: Saved as house\n"
        "Assistant: Saved as agent\n"
        "Assistant: Hello from the house\n"
        "Caller: Hi this is Sam"
    )


def test_caller_cumulative_updates_coalesce_after_final():
    """Progressive STT after `final` revises the last caller turn.

    xAI sends `input_audio_transcription.updated` as the cumulative transcript.
    A later update used to append a second Caller line once `completed` had
    marked the turn final. Soft Slate then showed nested duplicates.
    """
    session = _session()
    session.add_turn("caller", "Hi", final=False, cumulative=True)
    session.add_turn("caller", "Hi this", final=False, cumulative=True)
    session.add_turn("caller", "Hi this is", final=True)
    session.add_turn("caller", "Hi this is Sam", final=False, cumulative=True)
    session.add_turn("caller", "Hi this is Sam calling", final=False, cumulative=True)
    session.add_turn("caller", "Hi this is Sam calling about the sink", final=True)
    assert [t["text"] for t in session.turns] == ["Hi this is Sam calling about the sink"]
    assert session.turns[0]["final"] is True
    assert session.turns[0]["role"] == "caller"
    assert session.transcript_text() == "Caller: Hi this is Sam calling about the sink"

    # A shorter prefix of the same utterance replaces; it does not add a line.
    session.add_turn("caller", "Hi this is Sam", final=False, cumulative=True)
    assert [t["text"] for t in session.turns] == ["Hi this is Sam"]
    assert session.turns[0]["final"] is True

    # Superset that is not a raw prefix (words added in front and back).
    session.add_turn(
        "caller",
        "Yeah, Hi this is Sam from Oak Street.",
        final=False,
        cumulative=True,
    )
    assert [t["text"] for t in session.turns] == ["Yeah, Hi this is Sam from Oak Street."]

    # Empty partials do not wipe the line or append a blank turn.
    session.add_turn("caller", "", final=False, cumulative=True)
    session.add_turn("caller", "   ", final=False, cumulative=True)
    assert [t["text"] for t in session.turns] == ["Yeah, Hi this is Sam from Oak Street."]

    # A different utterance appends, including across an assistant reply.
    session.add_turn("assistant", "How can I help", final=True)
    session.add_turn("caller", "The kitchen sink is leaking", final=True)
    assert [(t["role"], t["text"]) for t in session.turns] == [
        ("caller", "Yeah, Hi this is Sam from Oak Street."),
        ("assistant", "How can I help"),
        ("caller", "The kitchen sink is leaking"),
    ]

    # An extension of the last caller line still replaces when a house line
    # sits above it. Final stays set so the next utterance cannot reopen it.
    session.add_turn("assistant", "Which", final=False)
    session.add_turn("assistant", " sink?", final=False)
    session.add_turn("caller", "The kitchen sink is leaking badly", final=False, cumulative=True)
    assert session.turns[2]["text"] == "The kitchen sink is leaking badly"
    assert session.turns[2]["final"] is True
    assert session.turns[-1]["role"] == "assistant" and session.turns[-1]["final"] is False
    assert len(session.turns) == 4

    # The done transcript replaces the open house line. A later response appends.
    session.add_turn("assistant", "Which sink?", final=True)
    session.add_turn("assistant", "Okay", final=True)
    assert [(t["role"], t["text"]) for t in session.turns] == [
        ("caller", "Yeah, Hi this is Sam from Oak Street."),
        ("assistant", "How can I help"),
        ("caller", "The kitchen sink is leaking badly"),
        ("assistant", "Which sink?"),
        ("assistant", "Okay"),
    ]

    # DTMF is its own caller turn.
    session.add_turn("caller", "[dtmf 1]", final=True)
    assert session.turns[-1]["text"] == "[dtmf 1]"
    assert sum(1 for t in session.turns if t["role"] == "caller") == 3


def test_xai_transcription_events_coalesce_caller_lines():
    async def go():
        session = _session()
        await session.on_xai({
            "type": "conversation.item.input_audio_transcription.updated",
            "transcript": "Hi",
        })
        await session.on_xai({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Hi this is",
        })
        await session.on_xai({
            "type": "conversation.item.input_audio_transcription.updated",
            "transcript": "Hi this is Sam",
        })
        await session.on_xai({
            "type": "conversation.item.input_audio_transcription.updated",
            "transcript": "",
        })
        await session.on_xai({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Hi this is Sam",
        })
        await session.on_xai({
            "type": "response.output_audio_transcript.delta",
            "delta": "Hello",
        })
        await session.on_xai({
            "type": "response.output_audio_transcript.delta",
            "delta": " from the house",
        })
        await session.on_xai({
            "type": "response.output_audio_transcript.done",
            "transcript": "Hello from the house",
        })
        assert [(t["role"], t["text"]) for t in session.turns] == [
            ("caller", "Hi this is Sam"),
            ("assistant", "Hello from the house"),
        ]
        assert session.transcript_text() == (
            "Caller: Hi this is Sam\nAssistant: Hello from the house"
        )
        assert session.turns[1]["role"] == "assistant"

    asyncio.run(go())


def test_docs_do_not_call_the_mix_future_work():
    docs = (ROOT / "twilio_house_voice" / "DOCS.md").read_text()
    readme = (ROOT / "twilio_house_voice" / "README.md").read_text()
    assert "not in this version" not in docs.lower()
    assert "CallCatch" in readme
    assert "both sides" in readme



# --------------------------------------------------------------------------
# trust gate (0.2.0): StirVerstat, allow / block lists, /api/trust
# --------------------------------------------------------------------------

import trust  # noqa: E402


def test_stir_parse_is_signal_only():
    assert trust.parse_stir("TN-Validation-Passed-A") == {"status": "passed", "level": "A", "raw": "TN-Validation-Passed-A"}
    assert trust.parse_stir("TN-Validation-Passed-B-Diverted")["level"] == "B"
    assert trust.parse_stir("TN-Validation-Failed-C")["status"] == "failed"
    assert trust.parse_stir("TN-Validation-Failed")["level"] == ""
    assert trust.parse_stir("No-TN-Validation")["status"] == "none"
    assert trust.parse_stir("")["status"] == "absent"
    assert trust.parse_stir("<script>")["status"] == "absent"
    tmp = tempfile.mkdtemp()
    lists = trust.TrustLists(pathlib.Path(tmp) / "t.json")
    # classify takes no STIR input at all: a failed attestation cannot block.
    assert trust.classify(lists, "+12025550101") == "screen"


def test_trust_lists_normalize_move_and_persist():
    tmp = pathlib.Path(tempfile.mkdtemp())
    lists = trust.TrustLists(tmp / "t.json")
    entry, prev = lists.add("allow", "(202) 555-0101", "Mom")
    assert entry["number"] == "+12025550101" and prev == ""
    assert trust.classify(lists, "+1 202 555 0101") == "allow"
    entry, prev = lists.add("block", "12025550101")
    assert prev == "allow"
    assert trust.classify(lists, "2025550101") == "block"
    assert lists.snapshot()["allow"] == []
    again = trust.TrustLists(tmp / "t.json")
    assert [e["number"] for e in again.snapshot()["block"]] == ["+12025550101"]
    assert again.remove("block", "+12025550101") is True
    assert again.remove("block", "+12025550101") is False
    assert trust.classify(again, "+12025550101") == "screen"
    for bad in ("", "12", "abc"):
        try:
            again.add("block", bad)
            raise AssertionError("accepted " + repr(bad))
        except ValueError:
            pass
    try:
        again.add("vip", "+12025550101")
        raise AssertionError("accepted bad list")
    except ValueError:
        pass
    (tmp / "bad.json").write_text("{nope")
    assert trust.TrustLists(tmp / "bad.json").snapshot() == {"allow": [], "block": []}
    assert (tmp / "bad.corrupt").exists()


def test_voice_webhook_rejects_blocked_and_records_stir():
    async def go():
        hooks = []

        async def hook(request):
            hooks.append(await request.json())
            return web.json_response({"ok": True})

        hook_app = web.Application()
        hook_app.router.add_post("/hook", hook)
        hook_server = TestServer(hook_app)
        await hook_server.start_server()
        tmp = tempfile.mkdtemp()
        try:
            app = server.create_app(_opts(
                tmp, ha_webhook_transcript=str(hook_server.make_url("/hook")),
                ha_webhook_token="hook-token-secret",
            ), data_dir=tmp)
            app["trust"].add("block", "+12025550111")
            async with TestClient(TestServer(app)) as client:
                blocked = await _post_voice(client, call_id="CAblocked00000001", From="+12025550111",
                                            StirVerstat="TN-Validation-Passed-A")
                xml = await blocked.text()
                assert "<Reject" in xml and "<Stream" not in xml, xml
                st = await _signed_post(client, "/twilio/status", {
                    "CallSid": "CAblocked00000001", "CallStatus": "busy", "From": "+12025550111"})
                assert st.status == 200
                await asyncio.sleep(0.2)
                assert hooks == []
                assert not (pathlib.Path(tmp) / "voicemail" / "CAblocked00000001.json").exists()

                # STIR failed + unknown number: still screened, never rejected.
                got = await _post_voice(client, StirVerstat="TN-Validation-Failed-C")
                xml = await got.text()
                assert "<Reject" not in xml and "<Stream" in xml
                path, token = stream_path(xml)
                ws = await client.ws_connect(path)
                await ws.send_json(media_start("MZ1234567890abcd", token))
                await ws.send_json({"event": "media", "media": {"track": "inbound", "payload": SILENCE}})
                await ws.send_json({"event": "stop"})
                assert await wait_for(lambda: len(hooks) == 1)
                await ws.close()
                body = hooks[0]
                assert body["stir_verstat"] == "TN-Validation-Failed-C"
                assert body["stir"]["status"] == "failed"
                assert body["trust_verdict"] == "screen"
                side = json.loads((pathlib.Path(tmp) / "voicemail" / f"{CALL}.json").read_text())
                assert side["stir"]["level"] == "C"
                status = await (await client.get("/api/status")).json()
                assert status["blocked_calls"] == 1
                assert status["trust_counts"] == {"allow": 0, "block": 1}
                assert "+12025550111" not in json.dumps(status)

                # Missed call (no stream) still carries the attestation.
                await _post_voice(client, call_id="CAmissed000000001", From="+12025550122",
                                  StirVerstat="TN-Validation-Passed-B")
                await _signed_post(client, "/twilio/status", {
                    "CallSid": "CAmissed000000001", "CallStatus": "no-answer", "From": "+12025550122"})
                assert await wait_for(lambda: len(hooks) == 2)
                assert hooks[1]["stir"] == {"status": "passed", "level": "B", "raw": "TN-Validation-Passed-B"}
        finally:
            await hook_server.close()

    asyncio.run(go())


def test_allow_listed_caller_skips_screening_note():
    tmp = tempfile.mkdtemp()
    app = server.create_app(_opts(tmp), data_dir=tmp)
    app["trust"].add("allow", "+12025550101")
    assert trust.classify(app["trust"], "+12025550101") == "allow"
    session = server.CallSession(app, CALL, {"from": "+12025550101", "trust_verdict": "allow",
                                             "stir_verstat": "TN-Validation-Passed-A"})
    assert session.trust_verdict == "allow"
    assert session.stir_verstat == "TN-Validation-Passed-A"
    assert "allow list" in server.ALLOW_NOTE
    assert "allow list" not in server.DEFAULT_INSTRUCTIONS


def test_trust_api_requires_ingress_user_or_bearer():
    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app(_opts(tmp, trust_api_token="trust-token-0123456789"), data_dir=tmp)
        async with TestClient(TestServer(app)) as client:
            # Loopback but no user header and no bearer.
            assert (await client.get("/api/trust")).status == 403
            assert (await client.get("/api/trust", headers={"Authorization": "Bearer wrong"})).status == 403
            assert (await client.post("/api/trust/add", json={"list": "block", "number": "+12025550111"})).status == 403
            good = {"Authorization": "Bearer trust-token-0123456789"}
            add = await client.post("/api/trust/add", json={"list": "block", "number": "202-555-0111"}, headers=good)
            assert add.status == 200
            body = await add.json()
            assert body["entry"]["number"] == "+12025550111" and body["previous"] == ""
            bad = await client.post("/api/trust/add", json={"list": "block", "number": "x"}, headers=good)
            assert bad.status == 400
            junk = await client.post("/api/trust/add", data="nope", headers=good)
            assert junk.status == 400
            user = {"X-Remote-User-Id": "admin-1"}
            listed = await (await client.get("/api/trust", headers=user)).json()
            assert [e["number"] for e in listed["block"]] == ["+12025550111"]
            rm = await client.post("/api/trust/remove", json={"list": "block", "number": "+12025550111"}, headers=user)
            assert (await rm.json())["removed"] is True
        # No token configured: bearer path is closed even with an empty bearer.
        tmp2 = tempfile.mkdtemp()
        app2 = server.create_app(_opts(tmp2), data_dir=tmp2)
        async with TestClient(TestServer(app2)) as client:
            assert (await client.get("/api/trust", headers={"Authorization": "Bearer "})).status == 403

    asyncio.run(go())


def test_trust_token_is_redacted():
    opts = server.Opts({"trust_api_token": "trust-token-0123456789"})
    assert "trust-token-0123456789" in opts.secret_values()
    assert "trust-token-0123456789" not in server.redact("x trust-token-0123456789 y", opts.secret_values())



def test_trust_review_lows_l1_to_l4():
    # L1: only STIR passed-A rejects a blocked number; anything else screens, tagged.
    for verstat, action in (("TN-Validation-Passed-A", "reject"), ("TN-Validation-Passed-B", "screen"),
                            ("TN-Validation-Failed-A", "screen"), ("No-TN-Validation", "screen"), ("", "screen")):
        assert trust.block_action("block", trust.parse_stir(verstat)) == action, verstat
    assert trust.block_action("screen", trust.parse_stir("TN-Validation-Failed-C")) == "none"
    assert trust.block_action("allow", trust.parse_stir("TN-Validation-Passed-A")) == "none"

    async def go():
        tmp = tempfile.mkdtemp()
        app = server.create_app(_opts(tmp), data_dir=tmp)
        app["trust"].add("block", "+12025550111")
        async with TestClient(TestServer(app)) as client:
            got = await _post_voice(client, call_id="CAspoof000000001", From="+12025550111",
                                    StirVerstat="TN-Validation-Passed-C")
            xml = await got.text()
            assert "<Reject" not in xml and "<Stream" in xml
            assert app["call_meta"]["CAspoof000000001"]["trust_verdict"] == "blocked"

    asyncio.run(go())

    # L2: a full target list never drops the number from the other list.
    tmp = pathlib.Path(tempfile.mkdtemp())
    lists = trust.TrustLists(tmp / "t.json")
    lists.add("allow", "+12025550199")
    for i in range(trust.MAX_ENTRIES):
        lists._data["block"].append({"number": f"+1555{i:07d}", "note": "", "added_at": ""})
    try:
        lists.add("block", "+12025550199")
        raise AssertionError("expected list-full")
    except ValueError as exc:
        assert str(exc) == "list-full"
    assert trust.classify(lists, "+12025550199") == "allow"

    # L3: non-+1 numbers keep every digit and never collide with a US number.
    assert trust.number_key("+44 20 7946 0123") == "+442079460123"
    assert trust.number_key("+2025550123") == "+2025550123"
    assert trust.e164("+2025550123") == "+2025550123"
    assert trust.number_key("+2025550123") != trust.number_key("+12025550123")
    assert trust.e164("(202) 555-0123") == "+12025550123"

    # L4: the Docker gateway never gets the bearer path.
    class Req:
        def __init__(self, remote):
            self.remote = remote
            self.headers = {"Authorization": "Bearer tok-555"}
            self.app = {"opts": type("O", (), {"trust_api_token": "tok-555"})()}

    assert server.trust_authorized(Req("172.30.33.4")) is True
    assert server.trust_authorized(Req("127.0.0.1")) is True
    assert server.trust_authorized(Req("172.30.32.1")) is False
    assert server.trust_authorized(Req("192.168.1.5")) is False

def test_stdin_block_unblock_and_dry_run():
    tmp = tempfile.mkdtemp()
    app = server.create_app(_opts(tmp), data_dir=tmp)
    lists = app["trust"]
    # Supervisor may write JSON with or without a newline; values can arrive split.
    values, rest = server._stdin_values('{"cmd": "block", "number": "+12025550133"}{"cmd": "unbl')
    assert values == [{"cmd": "block", "number": "+12025550133"}] and rest.startswith('{"cmd"')
    values, rest = server._stdin_values(rest + 'ock", "number": "+12025550133"}\n')
    assert values == [{"cmd": "unblock", "number": "+12025550133"}] and rest == ""
    dry = server.handle_stdin_command(app, {"cmd": "block", "number": "+12025550133", "dry_run": True})
    assert dry["dry_run"] is True and trust.classify(lists, "+12025550133") == "screen"
    got = server.handle_stdin_command(app, '{"cmd": "block", "number": "+12025550133"}')
    assert got["ok"] and trust.classify(lists, "+12025550133") == "block"
    got = server.handle_stdin_command(app, {"cmd": "unblock", "number": "+12025550133"})
    assert got["removed"] is True and trust.classify(lists, "+12025550133") == "screen"
    # Only block / unblock. No allow, no options, no arbitrary keys.
    assert server.handle_stdin_command(app, {"cmd": "allow", "number": "+12025550133"})["error"] == "unknown-cmd"
    assert server.handle_stdin_command(app, {"cmd": "block", "number": ""})["error"] == "bad-number"
    assert server.handle_stdin_command(app, ["block"])["error"] == "bad-command"
    assert server.handle_stdin_command(app, "not json")["error"] == "bad-json"
    assert server.stdin_enabled() is False  # tests run outside HA
    config = (ROOT / "twilio_house_voice" / "config.yaml").read_text()
    assert re.search(r"(?m)^stdin:\s*true\s*$", config)

    async def go():
        import io
        stream = io.StringIO('{"cmd":"block","number":"2025550144"}\n')
        await server.stdin_loop(app, stream)
        assert trust.classify(lists, "+12025550144") == "block"
        # M1: an int number (HA template output) is coerced, and a command that
        # raises does not stop the loop.
        assert server.handle_stdin_command(app, {"cmd": "unblock", "number": 12025550144})["removed"] is True
        dry = server.handle_stdin_command(app, {"cmd": "block", "number": 12025550144, "dry_run": True})
        assert dry["dry_run"] is True
        for junk in (True, 1.5, ["x"], {"a": 1}, None):
            assert server.handle_stdin_command(app, {"cmd": "block", "number": junk})["error"] == "bad-number"
        real = server.handle_stdin_command
        calls = []

        def boom(app_, value):
            calls.append(value)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(app_, value)

        server.handle_stdin_command = boom
        try:
            stream = io.StringIO('{"cmd":"block","number":2025550166}\n{"cmd":"block","number":2025550177}\n')
            await server.stdin_loop(app, stream)
        finally:
            server.handle_stdin_command = real
        assert len(calls) == 2 and trust.classify(lists, "+12025550177") == "block"

    asyncio.run(go())


def test_plaid_ui_security_headers_and_no_inline_script():
    static = APP / "static"
    page = (static / "index.html").read_text()
    js = (static / "app.js").read_text()
    css = (static / "style.css").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", page), "inline <script> breaks the CSP"
    assert "<style" not in page and "style=" not in page
    assert not re.search(r"\son[a-z]+=", page)
    assert "innerHTML" not in js, "build DOM with textContent only"
    assert "CallCatch" not in page and "<title>Voice</title>" in page
    assert "--bg: #000" in css and "--tap: 48px" in css

    async def go():
        with tempfile.TemporaryDirectory() as tmp:
            app = server.create_app({"xai_bridge": "stub", "log_level": "error"}, data_dir=tmp)

            async def boom(_request):
                raise RuntimeError("secret detail")

            app.router.add_get("/api/boom", boom)
            async with TestClient(TestServer(app)) as client:
                for path in ("/", "/static/app.js", "/static/style.css", "/api/status", "/api/boom", "/nope"):
                    resp = await client.get(path)
                    for key in server.SECURITY_HEADERS:
                        assert key in resp.headers, (path, key)
                    if path.startswith("/api/"):
                        assert resp.headers["Cache-Control"] == "no-store", path
                boom_resp = await client.get("/api/boom")
                assert boom_resp.status == 500
                assert "secret detail" not in await boom_resp.text()

    asyncio.run(go())


def test_signature_required_whenever_auth_token_set():
    url = "https://voice.example/twilio/voice"
    params = {"CallSid": CALL, "From": "+12025550111"}
    good = independent_signature(AUTH, url, params)
    for flag in (True, False):
        opts = server.Opts({"twilio_auth_token": AUTH, "validate_twilio_signature": flag})
        assert server.twilio_authorized(opts, "127.0.0.1", url, params, good)
        assert not server.twilio_authorized(opts, "127.0.0.1", url, params, "")
        assert not server.twilio_authorized(opts, "172.30.32.2", url, params, "bogus")
    no_token = server.Opts({"validate_twilio_signature": False})
    assert not server.twilio_authorized(no_token, "172.30.32.2", url, params, "")
    assert not server.twilio_authorized(no_token, "203.0.113.9", url, params, "")


def test_no_token_twilio_routes_503_and_token_needs_public_base_url():
    async def go():
        tmp = tempfile.mkdtemp()
        try:
            app = server.create_app({"xai_bridge": "stub", "log_level": "error",
                                     "public_base_url": "https://voice.example"}, data_dir=tmp)
            async with TestClient(TestServer(app)) as client:
                data = {"CallSid": CALL, "From": "+12025550101"}
                for path in ("/twilio/voice", "/twilio/status"):
                    r = await client.post(path, data=data, headers=_sig(path, data))
                    assert r.status == 503, (path, r.status)
                    assert await r.text() == "twilio not configured"
                ws = await client.get("/twilio/stream")
                assert ws.status == 503
                assert (await client.get("/api/health")).status == 200
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    asyncio.run(go())
    assert server.startup_config_error(server.Opts({})) == ""
    assert server.startup_config_error(server.Opts({"twilio_auth_token": AUTH, "public_base_url": "https://voice.example"})) == ""
    err = server.startup_config_error(server.Opts({"twilio_auth_token": AUTH}))
    assert "public_base_url is required" in err and AUTH not in err
    half = server.Opts({"twilio_auth_token": AUTH})
    assert server.twilio_unavailable(half)
    real_create, real_run = server.create_app, server.web.run_app
    ran = []
    server.create_app = lambda: real_create({"twilio_auth_token": AUTH, "log_level": "error"}, data_dir=tempfile.mkdtemp())
    server.web.run_app = lambda *a, **k: ran.append(1)
    try:
        try:
            server.main()
            raise AssertionError("main() started without public_base_url")
        except SystemExit as exc:
            assert exc.code == 2
        assert not ran
    finally:
        server.create_app, server.web.run_app = real_create, real_run


def test_public_base_url_must_be_bare_https_origin():
    ok = ["https://voice.example.com", "https://voice.example.com/", "https://voice.example.com:8443", "HTTPS://Voice.Example.com"]
    for url in ok:
        assert server.public_base_url_error(url) == "", url
        opts = server.Opts({"twilio_auth_token": AUTH, "public_base_url": url})
        assert server.startup_config_error(opts) == "", url
    bad = {
        "http://voice.example.com": "https://",
        "voice.example.com": "https://",
        "https://": "host",
        "https://voice.example.com/twilio": "path",
        "https://voice.example.com/a/b/": "path",
        "https://voice.example.com?x=1": "query",
        "https://voice.example.com/?x=1": "query",
        "https://voice.example.com#frag": "fragment",
        "https://user:hunter2-secret@voice.example.com": "credentials",
        "https://voice.example.com:notaport": "not a valid URL",
        "https://voice.example .com": "spaces",
        "https://voice\u200b.example.com": "ASCII",
        "https://voice.example.com\u2044twilio": "ASCII",
        "https://v\u00f6ice.example.com": "ASCII",
        "https://voice.example.com.": "dot",
        "https://voice.example.com.:8443": "dot",
        "https://voice.example.com:": "empty port",
    }
    for url, why in bad.items():
        for token in (AUTH, ""):
            opts = server.Opts({"twilio_auth_token": token, "public_base_url": url})
            err = server.startup_config_error(opts)
            assert why in err, (url, err)
            assert AUTH not in err and "hunter2-secret" not in err, err
    real_create, real_run = server.create_app, server.web.run_app
    ran, logged = [], []

    class Grab(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    handler = Grab()
    server.log.addHandler(handler)
    server.create_app = lambda: real_create(
        {"twilio_auth_token": AUTH, "log_level": "error",
         "public_base_url": "https://user:hunter2-secret@voice.example.com/twilio?x=1"},
        data_dir=tempfile.mkdtemp())
    server.web.run_app = lambda *a, **k: ran.append(1)
    try:
        try:
            server.main()
            raise AssertionError("main() started with a bad public_base_url")
        except SystemExit as exc:
            assert exc.code == 2
        assert not ran
        text = "\n".join(logged)
        assert "refusing to start" in text and "public_base_url" in text, text
        assert "hunter2-secret" not in text and AUTH not in text, text
    finally:
        server.log.removeHandler(handler)
        server.create_app, server.web.run_app = real_create, real_run


def test_031_health_via_docker_healthcheck_not_watchdog_key():
    root = pathlib.Path(__file__).resolve().parents[1]
    cfg = (root / "config.yaml").read_text()
    assert not re.search(r"^watchdog\s*:", cfg, re.M), "obsolete watchdog: key is back"
    version = re.search(r'^version:\s*"([^"]+)"', cfg, re.M).group(1)
    dockerfile = (root / "Dockerfile").read_text()
    hc = re.search(r"^HEALTHCHECK\b.*?CMD (.+)$", dockerfile, re.M | re.S)
    assert hc, "Dockerfile has no HEALTHCHECK"
    assert "http://127.0.0.1:8787/api/health" in hc.group(1)
    assert str(server.PORT) == "8787"
    assert 'CMD ["python3", "-u", "/app/server.py"]' in dockerfile
    assert f'org.opencontainers.image.version="{version}"' in dockerfile


if __name__ == "__main__":
    os.environ.pop("SUPERVISOR_TOKEN", None)
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("OK")
