"""0.1.2: caller number + call start as the call key (xAI console variables).  Fake data only."""
from __future__ import annotations

import logging
import time

from test_line import AUTH, CALLER, call_tool, jobs, rpc

START = "2026-11-17T22:30:05Z"  # 17:30 ET


async def init_vars(client, caller=CALLER, start=START, call_id=None):
    headers = dict(AUTH)
    if caller is not None:
        headers["X-CallCatch-From"] = caller
    if start is not None:
        headers["X-CallCatch-Start"] = start
    if call_id:
        headers["X-CallCatch-Call"] = call_id
    r = await client.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                          "clientInfo": {"name": "test", "version": "0"}}),
                          headers=headers)
    assert r.status == 200
    headers["Mcp-Session-Id"] = r.headers["Mcp-Session-Id"]
    return headers


def rows(cc_mod, cc_app):
    calls, _ = cc_mod.ensure_agent(cc_app)
    return calls.all()


async def test_initialize_with_caller_and_start_records_the_call(stack):
    line, cc_app, cc_mod, _ = stack
    await init_vars(line)
    r = rows(cc_mod, cc_app)
    assert len(r) == 1 and r[0]["call_start"] == START and r[0]["fields"]["caller_id"] == CALLER


async def test_two_sessions_same_caller_and_start_are_one_call(stack):
    line, cc_app, cc_mod, _ = stack
    h1 = await init_vars(line)
    await call_tool(line, h1, "update_call", {"caller_name": "Pat Example"})
    h2 = await init_vars(line)  # the agent reconnected mid-call
    await call_tool(line, h2, "update_call", {"turn": {"speaker": "caller", "text": "still me"}}, rid=3)
    r = rows(cc_mod, cc_app)
    assert len(r) == 1 and r[0]["fields"]["caller_name"] == "Pat Example" and r[0]["turns"][0]["text"] == "still me"
    # Same caller, a later call: a new row.
    h3 = await init_vars(line, start="2026-11-17T23:10:00Z")
    await call_tool(line, h3, "update_call", {"reason": "second call"}, rid=4)
    assert len(rows(cc_mod, cc_app)) == 2


async def test_lost_session_maps_back_by_caller_and_start(stack, line_mod):
    line, cc_app, cc_mod, _ = stack
    h = await init_vars(line)
    await call_tool(line, h, "update_call", {"reason": "before restart"})
    line.server.app[line_mod.APP]["sessions"].clear()  # Line restarted
    await call_tool(line, h, "update_call", {"turn": {"speaker": "caller", "text": "after restart"}}, rid=5)
    r = rows(cc_mod, cc_app)
    assert len(r) == 1 and r[0]["turns"][0]["text"] == "after restart"


async def test_unfilled_or_odd_start_falls_back_to_session(stack):
    line, cc_app, cc_mod, _ = stack
    for i, bad in enumerate(["{{call_start_time}}", "x" * 65, "2026<11>17"]):
        h = await init_vars(line, start=bad)
        await call_tool(line, h, "update_call", {"reason": f"r{i}"}, rid=10 + i)
    r = rows(cc_mod, cc_app)
    assert all(not row.get("call_start") for row in r)
    assert len({row["key"] for row in r}) == len(r)


async def test_start_without_caller_is_not_a_key(stack):
    line, cc_app, cc_mod, _ = stack
    h1 = await init_vars(line, caller=None)
    h2 = await init_vars(line, caller=None)
    await call_tool(line, h1, "update_call", {"reason": "a"}, rid=2)
    await call_tool(line, h2, "update_call", {"reason": "b"}, rid=3)
    assert len(rows(cc_mod, cc_app)) == 2  # session keys, not merged


async def test_call_id_header_still_wins(stack):
    line, cc_app, cc_mod, _ = stack
    h1 = await init_vars(line, call_id="conv-A")
    h2 = await init_vars(line, call_id="conv-B")  # same caller + start, different conversation
    await call_tool(line, h1, "update_call", {"reason": "a"}, rid=2)
    await call_tool(line, h2, "update_call", {"reason": "b"}, rid=3)
    assert {r.get("conversation_id") for r in rows(cc_mod, cc_app)} == {"conv-A", "conv-B"}


async def test_email_shows_start_eastern_and_console_hint_last4(stack):
    line, cc_app, cc_mod, _ = stack
    h = await init_vars(line)
    _st, body = await call_tool(line, h, "take_message", {"reason": "quote"}, rid=2)
    ref = body["result"]["structuredContent"]["ref"]
    text = jobs(cc_mod, cc_app)[f"email:{ref}"]["payload"]["text"]
    assert "Call started: Tue Nov 17, 17:30 ET" in text
    assert "caller number ending 0177, call started Tue Nov 17, 17:30 ET" in text
    assert "5555550177" not in text and "555-0177" not in text


async def test_unknown_start_format_shown_as_sent(stack):
    line, cc_app, cc_mod, _ = stack
    h = await init_vars(line, start="17 Nov 2026 5:30 PM")
    _st, body = await call_tool(line, h, "take_message", {"reason": "quote"}, rid=2)
    ref = body["result"]["structuredContent"]["ref"]
    text = jobs(cc_mod, cc_app)[f"email:{ref}"]["payload"]["text"]
    assert "Call started: 17 Nov 2026 5:30 PM (as sent by the platform)" in text


async def test_epoch_start_and_finalizer_once(stack):
    line, cc_app, cc_mod, _ = stack
    h = await init_vars(line, start="1763418605")  # 2025-11-17T22:30:05Z
    await call_tool(line, h, "update_call", {"turn": {"speaker": "caller", "text": "hello"}}, rid=2)
    calls, _ = cc_mod.ensure_agent(cc_app)
    done = calls.sweep(180, False, 120, now=time.time() + 181)
    for row in done:
        await cc_mod.deliver_final(cc_app, row)
    assert len(done) == 1
    text = jobs(cc_mod, cc_app)[f"email:{done[0]['ref']}"]["payload"]["text"]
    assert "Call started: Mon Nov 17, 17:30 ET" in text
    assert calls.sweep(180, False, 120, now=time.time() + 9999) == []


async def test_logs_hold_no_start_or_full_number(stack, caplog):
    caplog.set_level(logging.DEBUG)
    line, *_ = stack
    h = await init_vars(line)
    await call_tool(line, h, "update_call", {"reason": "x"}, rid=2)
    assert START not in caplog.text and "5555550177" not in caplog.text and "0177" in caplog.text


async def test_date_only_start_from_same_number_is_two_calls(stack):
    """Audit L1: a coarse start (date only) must not join two calls."""
    line, cc_app, cc_mod, _ = stack
    h1 = await init_vars(line, start="2026-11-17")
    _st, b1 = await call_tool(line, h1, "take_message", {"reason": "morning call"}, rid=2)
    h2 = await init_vars(line, start="2026-11-17")  # same number, later that day
    _st, b2 = await call_tool(line, h2, "take_message", {"reason": "evening call"}, rid=3)
    r = rows(cc_mod, cc_app)
    assert len(r) == 2 and len({row["key"] for row in r}) == 2
    refs = {b1["result"]["structuredContent"]["ref"], b2["result"]["structuredContent"]["ref"]}
    assert len(refs) == 2
    js = jobs(cc_mod, cc_app)
    assert all(f"email:{ref}" in js for ref in refs)


async def test_zero_year_start_from_same_number_is_two_calls(stack):
    """Audit RG2-L1: an in-range-looking ISO start outside START_MIN..START_MAX
    must not join two calls (0001-01-01T00:00:00Z is one such value)."""
    line, cc_app, cc_mod, _ = stack
    h1 = await init_vars(line, start="0001-01-01T00:00:00Z")
    _st, b1 = await call_tool(line, h1, "take_message", {"reason": "first call"}, rid=2)
    h2 = await init_vars(line, start="0001-01-01T00:00:00Z")
    _st, b2 = await call_tool(line, h2, "take_message", {"reason": "second call"}, rid=3)
    r = rows(cc_mod, cc_app)
    assert len(r) == 2 and len({row["key"] for row in r}) == 2
    refs = {b1["result"]["structuredContent"]["ref"], b2["result"]["structuredContent"]["ref"]}
    assert len(refs) == 2
    js = jobs(cc_mod, cc_app)
    assert all(f"email:{ref}" in js for ref in refs)


async def test_minute_resolution_start_is_not_a_key(stack):
    line, cc_app, cc_mod, _ = stack
    for i in range(2):
        h = await init_vars(line, start="2026-11-17T22:30Z")
        await call_tool(line, h, "update_call", {"reason": f"r{i}"}, rid=2 + i)
    assert len(rows(cc_mod, cc_app)) == 2


def test_start_is_precise(line_mod):
    ok = ["2026-11-17T22:30:05Z", "2026-11-17 22:30:05", "2026-11-17T22:30:05.5+00:00", "1763418605",
          "1763418605000"]
    bad = ["2026-11-17", "2026-11-17T22:30", "2026-11-17T22:30Z", "17 Nov 2026 5:30 PM", "1763418",
           "9999999999", "2026-02-30T10:00:00Z", "{{call_start_time}}", "",
           "0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z", "2019-12-31T23:59:59Z", "9999-01-01T00:00:00Z"]
    assert all(line_mod.start_is_precise(s) for s in ok)
    assert not any(line_mod.start_is_precise(s) for s in bad)
