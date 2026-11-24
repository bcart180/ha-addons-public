"""0.2.0 Telnyx webhooks: Ed25519, replay window, fail closed.  Placeholder key only.

0.2.1: the path also needs a connection allowlist; CONN is a fake id.
"""
from __future__ import annotations

import base64
import json
import pathlib
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import AGENT_TOKEN
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

CONN = "1000000000000000001"


def _signed(priv, body: bytes, ts: str | None = None) -> tuple[bytes, dict]:
    stamp = ts if ts is not None else str(int(time.time()))
    sig = base64.b64encode(priv.sign(stamp.encode() + b"|" + body)).decode()
    headers = {
        "telnyx-signature-ed25519": sig,
        "telnyx-timestamp": stamp,
        "Content-Type": "application/json",
    }
    return body, headers


def _pub(priv) -> str:
    raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def _body() -> bytes:
    return json.dumps({
        "data": {
            "record_type": "event",
            "event_type": "call.hangup",
            "id": "77777777-7777-4777-8777-777777777777",
            "occurred_at": "2026-11-19T22:00:02Z",
            "payload": {
                "call_session_id": "11111111-1111-4111-8111-111111111111",
                "call_leg_id": "33333333-3333-4333-8333-333333333333",
                "call_control_id": "v3:test-control",
                "connection_id": CONN,
                "from": "+15555550100",
                "start_time": "2026-11-19T22:00:00Z",
                "end_time": "2026-11-19T22:00:02Z",
            },
        },
        "meta": {"attempt": 1},
    }).encode()


@pytest.fixture
def keys():
    return Ed25519PrivateKey.generate()


@pytest.mark.asyncio
async def test_missing_key_disables_the_path(line_mod):
    app = line_mod.make_app({"mcp_token": "m" * 32, "callcatch_agent_token": AGENT_TOKEN})
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/telnyx/events", data=_body(), headers={"Content-Type": "application/json"})
        assert resp.status == 503


@pytest.mark.asyncio
async def test_invalid_and_stale_signatures_are_rejected(line_mod, keys):
    app = line_mod.make_app({
        "mcp_token": "m" * 32,
        "callcatch_agent_token": AGENT_TOKEN,
        "telnyx_public_key": _pub(keys),
        "telnyx_connection_ids": CONN,
    })
    raw = _body()
    async with TestClient(TestServer(app)) as client:
        _body_bytes, headers = _signed(keys, raw)
        bad = dict(headers)
        sig = bytearray(base64.b64decode(bad["telnyx-signature-ed25519"]))
        sig[0] ^= 0x01
        bad["telnyx-signature-ed25519"] = base64.b64encode(bytes(sig)).decode()
        invalid = await client.post("/telnyx/events", data=raw, headers=bad)
        assert invalid.status == 401

        stale_body, stale_headers = _signed(keys, raw, ts=str(int(time.time()) - 301))
        stale = await client.post("/telnyx/events", data=stale_body, headers=stale_headers)
        assert stale.status == 401

        future_body, future_headers = _signed(keys, raw, ts=str(int(time.time()) + 301))
        future = await client.post("/telnyx/events", data=future_body, headers=future_headers)
        assert future.status == 401

        missing = await client.post("/telnyx/events", data=raw, headers={"Content-Type": "application/json"})
        assert missing.status == 401


@pytest.mark.asyncio
async def test_bad_configured_key_fails_closed(line_mod):
    app = line_mod.make_app({
        "mcp_token": "m" * 32,
        "callcatch_agent_token": AGENT_TOKEN,
        "telnyx_public_key": "not-a-key",
        "telnyx_connection_ids": CONN,
    })
    raw, headers = _signed(Ed25519PrivateKey.generate(), _body())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/telnyx/events", data=raw, headers=headers)
        assert resp.status == 401


@pytest.mark.asyncio
async def test_valid_signature_is_not_rejected(line_mod, keys):
    """Verification succeeds.  Upstream is down, so the relay returns 502, not 401."""
    app = line_mod.make_app({
        "mcp_token": "m" * 32,
        "callcatch_agent_token": AGENT_TOKEN,
        "callcatch_url": "http://localhost:8099",
        "telnyx_public_key": _pub(keys),
        "telnyx_connection_ids": CONN,
    })
    raw, headers = _signed(keys, _body())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/telnyx/events", data=raw, headers=headers)
        assert resp.status == 502


def test_strict_parser_drops_unknown_and_public_urls(line_mod, keys):
    body = json.loads(_body())
    body["data"]["event_type"] = "call.recording.saved"
    body["data"]["payload"]["recording_id"] = "22222222-2222-4222-8222-222222222222"
    body["data"]["payload"]["channels"] = "dual"
    body["data"]["payload"]["recording_urls"] = {"wav": "https://s3.amazonaws.com/telephony-recorder-prod/t.wav"}
    body["data"]["payload"]["public_recording_urls"] = {"wav": "https://evil.example/secret.wav"}
    event = line_mod._telnyx_event(body)
    assert event["channels"] == "dual"
    assert "public_recording_urls" not in event
    assert event["recording_urls"]["wav"].startswith("https://s3.amazonaws.com/")
    body["data"]["record_type"] = "other"
    assert line_mod._telnyx_event(body) is None
    body["data"]["record_type"] = "event"
    body["data"]["event_type"] = "call.bridged"
    assert line_mod._telnyx_event(body) is None


def test_public_key_format_is_documented_and_library_is_pinned():
    root = pathlib.Path(__file__).resolve().parents[1]
    docs = (root / "DOCS.md").read_text(encoding="utf-8")
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "32-byte" in docs and "base64" in docs
    assert "py3-cryptography>=46.0.7" in docker
    assert "py3-cryptography=46.0.7-r0" not in docker


def test_telnyx_event_forwards_cnam_and_skips_numeric_names(line_mod):
    body = json.loads(_body())
    body["data"]["payload"]["caller_id_name"] = "Avery Example"
    event = line_mod._telnyx_event(body)
    assert event["from"] == "+15555550100"
    assert event["from_display"] == "Avery Example"
    body["data"]["payload"]["caller_id_name"] = "555 555 0100"
    event = line_mod._telnyx_event(body)
    assert "from_display" not in event
    body["data"]["payload"]["from"] = {
        "phone_number": "+15555550100",
        "display_name": "Sample Plumbing Co",
    }
    body["data"]["payload"].pop("caller_id_name", None)
    event = line_mod._telnyx_event(body)
    assert event["from"] == "+15555550100"
    assert event["from_display"] == "Sample Plumbing Co"
