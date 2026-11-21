"""0.2.1 connection allowlist on POST /telnyx/events.  Placeholder keys and fake ids only.

OTHER_CONN stands in for another app on the same Telnyx account (for example an
outbound-only TeXML number).  Its events must never reach CallCatch.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from conftest import AGENT_TOKEN
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from test_telnyx import CONN, _body, _pub, _signed

OTHER_CONN = "1000000000000000002"


@pytest.fixture
def keys():
    return Ed25519PrivateKey.generate()


@pytest.fixture
async def upstream():
    """A stand-in for CallCatch that records every relayed event."""
    seen: list[dict] = []

    async def telnyx(request: web.Request) -> web.Response:
        seen.append(await request.json())
        return web.json_response({"ok": True, "duplicate": False})

    app = web.Application()
    app.router.add_post("/api/agent/telnyx", telnyx)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield f"http://localhost:{server.port}", seen
    finally:
        await server.close()


def _event_body(connection) -> bytes:
    body = json.loads(_body())
    if connection is None:
        body["data"]["payload"].pop("connection_id", None)
    else:
        body["data"]["payload"]["connection_id"] = connection
    return json.dumps(body).encode()


def _app(line_mod, keys, base: str, connections):
    opts = {
        "mcp_token": "m" * 32,
        "callcatch_agent_token": AGENT_TOKEN,
        "callcatch_url": base,
        "telnyx_public_key": _pub(keys),
    }
    if connections is not None:
        opts["telnyx_connection_ids"] = connections
    return line_mod.make_app(opts)


@pytest.mark.parametrize("raw, expected", [
    ("", frozenset()),
    (None, frozenset()),
    (CONN, frozenset({CONN})),
    (f"{CONN}, {OTHER_CONN}", frozenset({CONN, OTHER_CONN})),
    (f"{CONN},abc", frozenset()),
    (",".join(str(10**18 + n) for n in range(11)), frozenset()),
])
def test_parse_fails_closed(line_mod, raw, expected):
    assert line_mod._telnyx_connections(raw) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("connections", [None, "", "abc", f"{CONN},x"])
async def test_key_without_valid_allowlist_keeps_the_path_off(line_mod, keys, upstream, connections):
    base, seen = upstream
    async with TestClient(TestServer(_app(line_mod, keys, base, connections))) as client:
        raw, headers = _signed(keys, _event_body(CONN))
        resp = await client.post("/telnyx/events", data=raw, headers=headers)
        assert resp.status == 503
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("connection", [OTHER_CONN, int(OTHER_CONN), None, "", "not-an-id"])
async def test_signed_events_from_other_connections_are_not_relayed(line_mod, keys, upstream, connection, caplog):
    base, seen = upstream
    async with TestClient(TestServer(_app(line_mod, keys, base, CONN))) as client:
        raw, headers = _signed(keys, _event_body(connection))
        resp = await client.post("/telnyx/events", data=raw, headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True, "ignored": True}
    assert seen == []
    assert OTHER_CONN not in " ".join(caplog.messages)


@pytest.mark.asyncio
async def test_allowlisted_event_is_relayed_with_its_connection_id(line_mod, keys, upstream):
    base, seen = upstream
    async with TestClient(TestServer(_app(line_mod, keys, base, f"{OTHER_CONN},{CONN}"))) as client:
        raw, headers = _signed(keys, _event_body(CONN))
        resp = await client.post("/telnyx/events", data=raw, headers=headers)
        assert resp.status == 200
    assert len(seen) == 1
    assert seen[0]["event"]["connection_id"] == CONN
    assert seen[0]["event"]["event_type"] == "call.hangup"


@pytest.mark.asyncio
async def test_unsigned_event_is_still_rejected_before_the_allowlist(line_mod, keys, upstream):
    base, seen = upstream
    async with TestClient(TestServer(_app(line_mod, keys, base, CONN))) as client:
        resp = await client.post("/telnyx/events", data=_event_body(CONN),
                                 headers={"Content-Type": "application/json"})
        assert resp.status == 401
    assert seen == []


def test_config_schema_has_the_option_and_defaults_blank():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    config = (root / "config.yaml").read_text(encoding="utf-8")
    assert '  telnyx_connection_ids: ""\n' in config
    assert "  telnyx_connection_ids: match(^[0-9, ]{0,400}$)?\n" in config
    assert 'version: "0.2.1"' in config
