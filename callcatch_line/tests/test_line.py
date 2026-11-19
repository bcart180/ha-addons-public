"""CallCatch Line: auth, limits, MCP protocol, hangup paths, webhook, logging.  Fake data only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sys
import time

from conftest import AGENT_TOKEN, HOOK_TOKEN, MCP_TOKEN

AUTH = {"Authorization": f"Bearer {MCP_TOKEN}", "Accept": "application/json, text/event-stream"}
CALLER = "+15555550177"


def rpc(method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        body["id"] = rid
    if params is not None:
        body["params"] = params
    return body


async def init(client, call_id="conv-test-001", caller=CALLER):
    headers = dict(AUTH)
    if call_id:
        headers["X-CallCatch-Call"] = call_id
    if caller:
        headers["X-CallCatch-From"] = caller
    r = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "test", "version": "0"}}),
                          headers=headers)
    assert r.status == 200
    sid = r.headers["Mcp-Session-Id"]
    headers["Mcp-Session-Id"] = sid
    await client.post("/mcp", json=rpc("notifications/initialized", rid=None), headers=headers)
    return headers, await r.json()


async def call_tool(client, headers, name, args, rid=2):
    r = await client.post("/mcp", json=rpc("tools/call", {"name": name, "arguments": args}, rid), headers=headers)
    return r.status, await r.json()


def jobs(cc_mod, cc_app):
    _calls, outbox = cc_mod.ensure_agent(cc_app)
    return json.loads(outbox.path.read_text())["jobs"] if outbox.path.exists() else {}


# --- auth and transport -----------------------------------------------------

async def test_auth_origin_size_paths(stack):
    line, *_ = stack
    assert (await line.post("/mcp", json=rpc("ping"))).status == 401
    assert (await line.post("/mcp", json=rpc("ping"), headers={"Authorization": f"Bearer {AGENT_TOKEN}"})).status == 401
    assert (await line.post("/mcp", json=rpc("ping"), headers={**AUTH, "Origin": "https://evil.example"})).status == 403
    big = "x" * (17 * 1024)
    assert (await line.post("/mcp", data=big, headers={"Content-Type": "application/json"})).status == 413
    assert (await line.get("/mcp", headers=AUTH)).status == 405
    for path in ("/", "/api/agent/call", "/api/voicemails", "/sse", "/mcp/../api"):
        assert (await line.get(path, headers=AUTH)).status in (404, 405)
    assert (await line.get("/healthz")).status == 200


async def test_valid_token_is_never_locked_out_and_spoofed_header_is_ignored(stack):
    """M2: auth first, lockout only throttles failures; CF-Connecting-IP from an
    untrusted peer neither targets another client nor escapes the lockout."""
    line, *_ = stack
    for i in range(11):  # rotate the header: it is ignored, the TCP peer is the key
        await line.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer wrong", "CF-Connecting-IP": f"203.0.113.{i}"})
    r = await line.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer wrong", "CF-Connecting-IP": "198.51.100.99"})
    assert r.status == 429  # rotating the header does not escape
    headers, _ = await init(line)  # same peer, valid token: still served
    ok = await line.post("/mcp", json=rpc("ping"), headers={**headers, "CF-Connecting-IP": "203.0.113.1"})
    assert ok.status == 200


async def test_cf_connecting_ip_trusted_only_from_tunnel_peer(stack):
    _line, _app, _mod, make_line = stack
    line = await make_line(trusted_proxies="127.0.0.1/32")
    for _ in range(11):
        await line.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer wrong", "CF-Connecting-IP": "203.0.113.9"})
    r = await line.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer wrong", "CF-Connecting-IP": "203.0.113.9"})
    assert r.status == 429
    r = await line.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer wrong", "CF-Connecting-IP": "198.51.100.7"})
    assert r.status == 401  # a different real client is not affected
    headers, _ = await init(line)
    ok = await line.post("/mcp", json=rpc("ping"), headers={**headers, "CF-Connecting-IP": "203.0.113.9"})
    assert ok.status == 200  # even the locked client's address passes with the valid token


async def test_peer_outside_allowed_peers_is_refused(stack):
    """M4: only the Supervisor network (and own loopback) may connect."""
    _line, _app, _mod, make_line = stack
    line = await make_line(allowed_peers="10.99.0.0/16", hook_token=HOOK_TOKEN)
    assert (await line.post("/mcp", json=rpc("ping"), headers=AUTH)).status == 403
    assert (await line.post("/hooks/call-ended", json={}, headers={"Authorization": f"Bearer {HOOK_TOKEN}"})).status == 403
    assert (await line.get("/healthz")).status == 200  # container health check only


def test_config_publishes_no_host_port_and_binds_peers():
    import pathlib

    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    assert cfg["ports"] == {"8791/tcp": None}
    assert cfg["options"]["allowed_peers"].startswith("172.30.32.0/23")
    assert cfg["options"]["trusted_proxies"] == ""
    for flag in ("hassio_api", "homeassistant_api", "auth_api", "host_network", "ingress"):
        assert cfg[flag] is False


async def test_not_configured_and_shared_secret_refused(stack):
    _line, _app, _mod, make_line = stack
    for opts in ({"mcp_token": ""}, {"mcp_token": AGENT_TOKEN}, {"callcatch_url": "https://evil.example.com"}):
        c = await make_line(**opts)
        assert (await c.post("/mcp", json=rpc("ping"), headers=AUTH)).status in (401, 503)


async def test_initialize_list_and_protocol(stack):
    line, cc_app, cc_mod, _ = stack
    headers, body = await init(line)
    assert body["result"]["protocolVersion"] == "2025-06-18"
    assert body["result"]["capabilities"] == {"tools": {"listChanged": False}}
    r = await line.post("/mcp", json=rpc("tools/list"), headers=headers)
    names = [t["name"] for t in (await r.json())["result"]["tools"]]
    assert names == ["update_call", "take_message", "request_callback", "get_free_busy"]
    # initialize alone recorded the call (record exists before any tool / hangup)
    calls, _ = cc_mod.ensure_agent(cc_app)
    rows = calls.all()
    assert len(rows) == 1 and rows[0]["fields"]["caller_id"] == CALLER
    assert (await line.post("/mcp", json=rpc("ping"), headers=headers)).status == 200
    assert (await line.post("/mcp", json=rpc("nope/method"), headers=headers)).status == 200
    bad = await line.post("/mcp", json=rpc("ping"), headers={**AUTH, "Mcp-Session-Id": "unknown"})
    assert bad.status == 404


async def test_initialize_without_call_id_creates_no_phantom_call(stack):
    line, cc_app, cc_mod, _ = stack
    await init(line, call_id="", caller="")
    await init(line, call_id="{{conversation_id}}", caller="")  # unsubstituted template
    calls, _ = cc_mod.ensure_agent(cc_app)
    assert calls.all() == []


async def test_sse_only_accept_gets_event_stream(stack):
    line, *_ = stack
    headers, _ = await init(line)
    h = {**headers, "Accept": "text/event-stream"}
    r = await line.post("/mcp", json=rpc("ping"), headers=h)
    text = await r.text()
    assert r.headers["Content-Type"].startswith("text/event-stream") and text.startswith("event: message\ndata: ")


# --- tools -------------------------------------------------------------------

async def test_argument_validation(stack):
    line, *_ = stack
    headers, _ = await init(line)
    _st, body = await call_tool(line, headers, "delete_everything", {})
    assert body["error"]["code"] == -32602
    _st, body = await call_tool(line, headers, "take_message", {"reason": "x", "ssn": "1"})
    assert body["result"]["isError"] and "unknown argument" in body["result"]["content"][0]["text"]
    _st, body = await call_tool(line, headers, "take_message", {"reason": "x" * 281})
    assert body["result"]["isError"]
    _st, body = await call_tool(line, headers, "take_message", {"caller_name": "No Reason"})
    assert body["result"]["isError"]
    _st, body = await call_tool(line, headers, "take_message", {"reason": "x", "callback_number": "drop table"})
    assert body["result"]["isError"]
    _st, body = await call_tool(line, headers, "get_free_busy", {"days": 99})
    assert body["result"]["isError"]


async def test_take_message_returns_ref_only_and_notifies_once(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-normal")
    await call_tool(line, headers, "update_call", {"caller_name": "Riley Placeholder"})
    _st, body = await call_tool(line, headers, "take_message",
                               {"caller_name": "Riley Placeholder", "reason": "deck estimate", "urgency": "normal"})
    res = body["result"]
    assert not res["isError"]
    ref = res["structuredContent"]["ref"]
    assert res["content"][0]["text"] == f"Saved.  Reference {ref}.  Bryson will get the message."
    assert "deck" not in json.dumps(res)
    j = jobs(cc_mod, cc_app)
    assert set(j) == {f"email:{ref}", f"push:{ref}"}
    # A retry by the agent does not duplicate the email.
    await call_tool(line, headers, "take_message", {"reason": "deck estimate"}, rid=3)
    assert set(jobs(cc_mod, cc_app)) == {f"email:{ref}", f"push:{ref}"}


async def test_caller_hangs_up_before_take_message(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-hangup")
    await call_tool(line, headers, "update_call", {"caller_name": "Drew Testperson", "reason": "water bill"})
    # ...caller hangs up.  No take_message, no end_call, no webhook.
    calls, _ = cc_mod.ensure_agent(cc_app)
    done = calls.sweep(180, False, 120, now=time.time() + 181)
    for row in done:
        await cc_mod.deliver_final(cc_app, row)
    assert len(done) == 1
    ref = done[0]["ref"]
    mail = jobs(cc_mod, cc_app)[f"email:{ref}"]["payload"]
    assert "Caller hung up early." in mail["text"] and "water bill" in mail["text"]
    assert "Drew Testperson" in mail["subject"]


async def test_hangup_before_any_tool_still_records_from_initialize(stack):
    line, cc_app, cc_mod, _ = stack
    await init(line, call_id="conv-silent", caller=CALLER)
    calls, _ = cc_mod.ensure_agent(cc_app)
    done = calls.sweep(180, False, 120, now=time.time() + 181)
    for row in done:
        await cc_mod.deliver_final(cc_app, row)
    assert len(done) == 1 and done[0]["hung_up_early"]
    mail = jobs(cc_mod, cc_app)[f"email:{done[0]['ref']}"]["payload"]
    assert "0177" in mail["text"] and "555-0177" not in mail["text"]  # masked by default (M1)


async def test_finals_limited_per_call(stack):
    _line, _app, _mod, make_line = stack
    line = await make_line(finals_per_call=2)
    headers, _ = await init(line, call_id="conv-limit")
    for i in range(2):
        _, body = await call_tool(line, headers, "take_message", {"reason": f"r{i}"}, rid=10 + i)
        assert not body["result"]["isError"]
    _, body = await call_tool(line, headers, "take_message", {"reason": "r3"}, rid=20)
    assert body["result"]["isError"]


async def test_free_busy_passthrough_is_busy_only(stack):
    line, _cc_app, cc_mod, _ = stack

    async def fake_events(_session, entity, _s, _e):
        return [{"summary": "SECRET thing", "location": "SECRET", "start": {"dateTime": "2030-01-07T10:00:00-05:00"},
                 "end": {"dateTime": "2030-01-07T11:00:00-05:00"}}]

    cc_mod.ha_calendar_events = fake_events
    headers, _ = await init(line)
    _, body = await call_tool(line, headers, "get_free_busy", {"days": 3})
    blob = json.dumps(body)
    assert not body["result"]["isError"] and "SECRET" not in blob
    assert set(body["result"]["structuredContent"]) == {"tz", "from", "days", "window", "busy", "note"}


# --- webhook ------------------------------------------------------------------

def _sign(secret: bytes, wid: str, ts: int, raw: bytes) -> str:
    return "v1," + base64.b64encode(hmac.new(secret, f"{wid}.{ts}.".encode() + raw, hashlib.sha256).digest()).decode()


async def test_hook_bearer_late_and_duplicate(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-hook")
    await call_tool(line, headers, "update_call", {"caller_name": "Hook Tester"})
    calls, _ = cc_mod.ensure_agent(cc_app)
    for row in calls.sweep(180, False, 120, now=time.time() + 181):   # finalizer fires first
        await cc_mod.deliver_final(cc_app, row)
    hook = {"id": "evt_123", "type": "call.ended",
            "data": {"call_id": "conv-hook", "from": CALLER,
                     "transcript": [{"role": "user", "text": "Hi it is Hook Tester."}]}}
    assert (await line.post("/hooks/call-ended", json=hook)).status == 401
    for _ in range(2):  # late + duplicated
        r = await line.post("/hooks/call-ended", json=hook, headers={"Authorization": f"Bearer {HOOK_TOKEN}"})
        assert r.status == 200
    j = jobs(cc_mod, cc_app)
    emails = [k for k in j if k.startswith("email:")]
    assert len(emails) == 1                                     # exactly one email per call
    ref = emails[0].split(":", 1)[1]
    assert f"push_update:{ref}" in j                            # one masked "details added" push
    item = cc_mod.ensure_runtime(cc_app).get("pa-" + ref.lower())
    assert "Hook Tester" in item["transcript"]


async def test_hook_standard_webhooks_signature(stack):
    _line, cc_app, cc_mod, make_line = stack
    secret = b"0123456789abcdef0123456789abcdef"
    line = await make_line(hook_signing_secret="whsec_" + base64.b64encode(secret).decode())
    raw = json.dumps({"id": "evt_sig", "data": {"call_id": "conv-sig", "from": CALLER,
                                                "transcript": "Caller: signed."}}).encode()
    ts = int(time.time())
    good = {"webhook-id": "evt_sig", "webhook-timestamp": str(ts), "webhook-signature": _sign(secret, "evt_sig", ts, raw),
            "Content-Type": "application/json"}
    assert (await line.post("/hooks/call-ended", data=raw, headers=good)).status == 200
    stale = dict(good, **{"webhook-timestamp": str(ts - 1000),
                          "webhook-signature": _sign(secret, "evt_sig", ts - 1000, raw)})
    assert (await line.post("/hooks/call-ended", data=raw, headers=stale)).status == 401
    tampered = raw.replace(b"signed", b"forged")
    assert (await line.post("/hooks/call-ended", data=tampered, headers=good)).status == 401
    assert any(k.startswith("email:") for k in jobs(cc_mod, cc_app))


async def test_hook_disabled_without_secret(stack):
    _line, _app, _mod, make_line = stack
    line = await make_line()
    assert (await line.post("/hooks/call-ended", json={"data": {"call_id": "x"}},
                            headers={"Authorization": f"Bearer {HOOK_TOKEN}"})).status == 503


async def test_delete_ends_call_when_enabled(stack):
    _line, cc_app, cc_mod, make_line = stack
    line = await make_line(delete_ends_call=True)
    headers, _ = await init(line, call_id="conv-delete")
    await call_tool(line, headers, "update_call", {"caller_name": "Del Test"})
    r = await line.delete("/mcp", headers=headers)
    assert r.status == 204
    assert any(k.startswith("email:") for k in jobs(cc_mod, cc_app))


async def test_upstream_down_gives_safe_error(stack):
    _line, _app, _mod, make_line = stack
    line = await make_line(callcatch_url="http://localhost:19")
    headers, _ = await init(line, call_id="conv-down")
    _, body = await call_tool(line, headers, "take_message", {"reason": "anything"})
    assert body["result"]["isError"] and "try again" in body["result"]["content"][0]["text"]


async def test_logs_hold_no_tokens_numbers_or_caller_text(stack, caplog):
    caplog.set_level(logging.DEBUG)
    line, *_ = stack
    headers, _ = await init(line, call_id="conv-logs")
    await call_tool(line, headers, "take_message", {"caller_name": "Secretive Name", "reason": "private matter",
                                                    "callback_number": "555-555-0178"})
    await line.post("/mcp", json=rpc("ping"), headers={"Authorization": "Bearer wrong-token-value"})
    text = caplog.text
    for bad in (MCP_TOKEN, AGENT_TOKEN, "wrong-token-value", "Secretive", "private matter", "5555550177", "555-0178"):
        assert bad not in text, bad


# --- audit fixes: sessionless writes (M3), chunked bodies (L1), number key (L2) ---

async def _sessionless(line, name, args, rid, extra=None):
    h = {**AUTH, **(extra or {})}
    r = await line.post("/mcp", json=rpc("tools/call", {"name": name, "arguments": args}, rid), headers=h)
    return await r.json()


async def test_sessionless_writes_without_any_call_id_are_refused(stack):
    line, cc_app, cc_mod, _ = stack
    for i in range(5):
        body = await _sessionless(line, "update_call", {"caller_name": f"Probe {i}"}, 100 + i)
        assert body["result"]["isError"] and "no call id" in body["result"]["content"][0]["text"]
    body = await _sessionless(line, "take_message", {"reason": "spam"}, 200)
    assert body["result"]["isError"]
    calls, _ = cc_mod.ensure_agent(cc_app)
    assert calls.all() == [] and jobs(cc_mod, cc_app) == {}


async def test_sessionless_writes_with_caller_id_share_one_row(stack):
    line, cc_app, cc_mod, _ = stack
    for i in range(5):
        body = await _sessionless(line, "update_call", {"notes": f"detail {i}"}, 100 + i, {"X-CallCatch-From": CALLER})
        assert not body["result"]["isError"]
    calls, _ = cc_mod.ensure_agent(cc_app)
    assert len(calls.all()) == 1
    # A new call from the same number after a quiet gap gets its own row.
    line_mod = __import__("sys").modules["line_server"]
    st = line_mod._state(line.server.app)
    n, last = st["caller_segments"][CALLER]
    st["caller_segments"][CALLER] = (n, last - line_mod.CALLER_GAP_S - 1)
    await _sessionless(line, "update_call", {"notes": "new call"}, 300, {"X-CallCatch-From": CALLER})
    assert len(calls.all()) == 2


async def test_chunked_body_is_read_whole_and_capped(stack):
    import asyncio
    line, *_ = stack
    raw = json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"})).encode()

    async def split():
        yield raw[:20]
        await asyncio.sleep(0.02)
        yield raw[20:]

    r = await line.post("/mcp", data=split(), headers=AUTH)
    assert r.status == 200 and "Mcp-Session-Id" in r.headers

    async def oversize():
        yield b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        await asyncio.sleep(0.02)
        for _ in range(40):
            yield b" " * 1024

    r = await line.post("/mcp", data=oversize(), headers=AUTH)
    assert r.status == 413


async def test_chunked_signed_hook_verifies(stack):
    import asyncio
    _line, cc_app, cc_mod, make_line = stack
    secret = b"0123456789abcdef0123456789abcdef"
    line = await make_line(hook_signing_secret="whsec_" + base64.b64encode(secret).decode())
    raw = json.dumps({"id": "evt_chunk", "data": {"call_id": "conv-chunk", "from": CALLER, "transcript": "Caller: hi."}}).encode()
    ts = int(time.time())
    hdr = {"webhook-id": "evt_chunk", "webhook-timestamp": str(ts), "webhook-signature": _sign(secret, "evt_chunk", ts, raw)}

    async def gen():
        yield raw[:10]
        await asyncio.sleep(0.02)
        yield raw[10:]

    assert (await line.post("/hooks/call-ended", data=gen(), headers=hdr)).status == 200
    assert any(k.startswith("email:") for k in jobs(cc_mod, cc_app))


async def test_per_number_limit_keys_on_caller_id_not_spoken_number(stack):
    _line, _app, _mod, make_line = stack
    line = await make_line(finals_per_number_hour=3)
    results = []
    for i in range(5):
        headers, _ = await init(line, call_id=f"conv-num-{i}", caller=CALLER)
        _, body = await call_tool(line, headers, "take_message",
                                  {"reason": "r", "callback_number": f"555-555-01{10 + i}"}, rid=50 + i)
        results.append(body["result"]["isError"])
    assert results == [False, False, False, True, True]


async def test_no_caller_id_limits_per_call_with_shared_ceiling(stack):
    """N1: one withheld-number caller cannot block the next; a shared ceiling still applies."""
    _line, _app, _mod, make_line = stack
    line = await make_line(finals_per_number_hour=3, finals_per_call=2, finals_no_caller_id_hour=10)
    results = []
    for i in range(8):  # 8 different withheld calls: all saved (old shared bucket stopped at 6)
        headers, _ = await init(line, call_id=f"conv-anon-{i}", caller="")
        _, body = await call_tool(line, headers, "take_message",
                                  {"reason": "r", "callback_number": f"555-555-01{20 + i}"}, rid=70 + i)
        results.append(body["result"]["isError"])
    assert results == [False] * 8
    # Per call: a withheld caller retrying on the same call is still limited.
    headers, _ = await init(line, call_id="conv-anon-repeat", caller="")
    errs = []
    for i in range(3):
        _, body = await call_tool(line, headers, "take_message", {"reason": f"again {i}"}, rid=90 + i)
        errs.append(body["result"]["isError"])
    assert errs == [False, False, True]
    # Shared ceiling: 10 per hour across all withheld calls (8 + 2 used above).
    headers, _ = await init(line, call_id="conv-anon-over", caller="")
    _, body = await call_tool(line, headers, "take_message", {"reason": "over"}, rid=99)
    assert body["result"]["isError"] and "busy" in body["result"]["content"][0]["text"]


# --- 0.1.1: session lost mid-call (Line or HA restart) -----------------------

async def test_lost_session_is_recreated_for_same_call(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line)
    status, body = await call_tool(line, headers, "update_call", {"caller_name": "Test Caller"})
    assert status == 200 and not body["result"]["isError"]
    ref = body["result"]["structuredContent"]["ref"]
    # Simulate a restart: Line forgets every session.
    st = line.server.app[sys.modules["line_server"].APP]
    st["sessions"].clear()
    status, body = await call_tool(line, headers, "take_message", {"reason": "after restart"}, rid=3)
    assert status == 200 and not body["result"]["isError"]
    assert body["result"]["structuredContent"]["ref"] == ref  # same call row, not a new one
    calls, _ = cc_mod.ensure_agent(cc_app)
    rows = calls.all()
    assert len(rows) == 1 and rows[0]["agent_final"] and rows[0]["fields"]["reason"] == "after restart"
    assert headers["Mcp-Session-Id"] in st["sessions"]


async def test_lost_session_without_call_header_keys_on_session_id(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="")
    status, body = await call_tool(line, headers, "update_call", {"reason": "first"})
    ref = body["result"]["structuredContent"]["ref"]
    st = line.server.app[sys.modules["line_server"].APP]
    st["sessions"].clear()
    status, body = await call_tool(line, headers, "update_call", {"notes": "second"}, rid=3)
    assert status == 200 and body["result"]["structuredContent"]["ref"] == ref
    assert len(cc_mod.ensure_agent(cc_app)[0].all()) == 1


async def test_malformed_unknown_session_still_404(stack):
    line, *_ = stack
    for sid in ("unknown", "x" * 31, "x" * 33, "a" * 31 + "!"):
        r = await line.post("/mcp", json=rpc("tools/list"), headers={**AUTH, "Mcp-Session-Id": sid})
        assert r.status == 404
    r = await line.delete("/mcp", headers={**AUTH, "Mcp-Session-Id": "unknown"})
    assert r.status == 404


async def test_recreate_still_needs_token(stack):
    line, *_ = stack
    sid = "A" * 32
    r = await line.post("/mcp", json=rpc("tools/list"), headers={"Mcp-Session-Id": sid})
    assert r.status == 401
    st = line.server.app[sys.modules["line_server"].APP]
    assert sid not in st["sessions"]


async def test_lost_session_delete_ends_call(stack):
    _line, cc_app, cc_mod, make_line = stack
    line2 = await make_line(delete_ends_call=True)
    headers, _ = await init(line2, call_id="conv-delete-01")
    await call_tool(line2, headers, "update_call", {"reason": "hello"})
    st = line2.server.app[sys.modules["line_server"].APP]
    st["sessions"].clear()
    r = await line2.delete("/mcp", headers=headers)
    assert r.status == 204
    rows = cc_mod.ensure_agent(cc_app)[0].all()
    assert len(rows) == 1 and rows[0]["state"] == "final" and rows[0]["final_source"] == "ended"
