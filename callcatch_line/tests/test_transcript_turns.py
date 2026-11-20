"""0.1.2 + CallCatch 0.7.2: agent-written transcript turns, summary, conversation id.  Fake data only."""
from __future__ import annotations

import json
import logging
import time

from test_line import call_tool, init, jobs


async def _finalize_all(cc_mod, cc_app, after=181):
    calls, _ = cc_mod.ensure_agent(cc_app)
    done = calls.sweep(180, False, 120, now=time.time() + after)
    for row in done:
        await cc_mod.deliver_final(cc_app, row)
    return done


async def test_turns_summary_and_conversation_id_reach_one_email(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv_TEST-0001")
    turns = [("agent", "Hi, you've reached Bryson's line.  This call is recorded."),
             ("caller", "Hi, it's Riley about the deck estimate."),
             ("caller", "Hi, it's Riley about the deck estimate."),  # retried tool call: kept once
             ("caller", "Call me back at 555 555 0142 after five.")]
    for i, (who, text) in enumerate(turns):
        st, body = await call_tool(line, headers, "update_call", {"turn": {"speaker": who, "text": text}}, rid=10 + i)
        assert st == 200 and not body["result"]["isError"]
    _st, body = await call_tool(line, headers, "take_message",
                               {"caller_name": "Riley Placeholder", "reason": "deck estimate",
                                "summary": "Riley wants a deck estimate; call back after 5 PM."}, rid=30)
    ref = body["result"]["structuredContent"]["ref"]
    j = jobs(cc_mod, cc_app)
    assert set(j) == {f"email:{ref}", f"push:{ref}"}
    mail = j[f"email:{ref}"]["payload"]
    text = mail["text"]
    assert "Summary\nRiley wants a deck estimate; call back after 5 PM." in text
    assert "Caller: Hi, it's Riley about the deck estimate." in text
    assert text.count("about the deck estimate") == 1
    assert "Agent: Hi, you've reached Bryson's line." in text
    assert "xAI conversation: conv_TEST-0001" in text
    assert "written by the phone agent" in text
    # Masked by default: the spoken number in a turn shows last 4 only.
    assert "0142" in text and "555 555 0142" not in text and "5555550142" not in text
    push = j[f"push:{ref}"]["payload"]
    assert "deck" not in json.dumps(push) and "Riley wants" not in json.dumps(push)
    # The finalizer later does not send again.
    assert await _finalize_all(cc_mod, cc_app, after=3600) == []
    assert set(jobs(cc_mod, cc_app)) == {f"email:{ref}", f"push:{ref}"}


async def test_hangup_mid_call_keeps_turns_and_finalizer_sends_once(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-hangup-turns")
    await call_tool(line, headers, "update_call", {"turn": {"speaker": "caller", "text": "My furnace is out."}})
    done = await _finalize_all(cc_mod, cc_app)
    assert len(done) == 1
    mail = jobs(cc_mod, cc_app)[f"email:{done[0]['ref']}"]["payload"]
    assert "Caller hung up early." in mail["text"] and "Caller: My furnace is out." in mail["text"]


async def test_late_turn_updates_row_not_email(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-late")
    _st, body = await call_tool(line, headers, "take_message", {"reason": "quick question"})
    ref = body["result"]["structuredContent"]["ref"]
    await call_tool(line, headers, "update_call", {"turn": {"speaker": "caller", "text": "One more thing."}}, rid=5)
    await call_tool(line, headers, "update_call", {"turn": {"speaker": "caller", "text": "And another."}}, rid=6)
    j = jobs(cc_mod, cc_app)
    assert sorted(k for k in j if k.startswith("email")) == [f"email:{ref}"]
    assert [k for k in j if k.startswith("push_update")] == [f"push_update:{ref}"]  # at most one
    calls, _ = cc_mod.ensure_agent(cc_app)
    row = next(r for r in calls.all() if r["ref"] == ref)
    assert [t["text"] for t in row["turns"]] == ["One more thing.", "And another."]


async def test_turn_validation(stack):
    line, *_ = stack
    headers, _ = await init(line, call_id="conv-validate")
    bad = [
        {"turn": {"speaker": "system", "text": "x"}},
        {"turn": {"speaker": "caller", "text": ""}},
        {"turn": {"speaker": "caller", "text": "x" * 501}},
        {"turn": {"speaker": "caller", "text": "x", "role": "admin"}},
        {"turn": "Caller: hello"},
        {"summary": "x" * 1201},
    ]
    for i, args in enumerate(bad):
        _st, body = await call_tool(line, headers, "update_call", args, rid=40 + i)
        assert body["result"]["isError"], args


async def test_turns_rate_limited_per_call(stack):
    _line, _cc_app, _cc_mod, make_line = stack
    line = await make_line(turns_per_call_min=3, tool_calls_per_min=100)
    headers, _ = await init(line, call_id="conv-rate")
    errs = []
    for i in range(5):
        _st, body = await call_tool(line, headers, "update_call",
                                    {"turn": {"speaker": "caller", "text": f"turn {i}"}}, rid=50 + i)
        errs.append(body["result"]["isError"])
    assert errs == [False, False, False, True, True]
    other, _ = await init(line, call_id="conv-rate-2")
    _st, body = await call_tool(line, other, "update_call", {"turn": {"speaker": "caller", "text": "hi"}}, rid=60)
    assert not body["result"]["isError"]  # per call, not global


async def test_html_is_escaped_and_injection_is_inert(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-html")
    await call_tool(line, headers, "update_call",
                    {"turn": {"speaker": "caller", "text": '<script>alert(1)</script> <a href="http://x.example">hi</a>'}})
    _st, body = await call_tool(line, headers, "take_message",
                               {"reason": "test", "summary": "<img src=x onerror=alert(1)>"}, rid=3)
    ref = body["result"]["structuredContent"]["ref"]
    html = jobs(cc_mod, cc_app)[f"email:{ref}"]["payload"]["html"]
    assert "<script>" not in html and "<img" not in html and 'href="http://x.example"' not in html


async def test_unfilled_or_odd_conversation_header_not_passed(stack):
    line, cc_app, cc_mod, make_line = stack
    for i, value in enumerate(["{{conversation_id}}", "a b", "x" * 101]):
        headers, _ = await init(line, call_id=value)
        _st, body = await call_tool(line, headers, "take_message", {"reason": f"r{i}"}, rid=70 + i)
        ref = body["result"]["structuredContent"]["ref"]
        calls, _ = cc_mod.ensure_agent(cc_app)
        row = next(r for r in calls.all() if r["ref"] == ref)
        assert row.get("conversation_id", "") == ""
    off = await make_line(send_conversation_id=False)
    headers, _ = await init(off, call_id="conv-not-sent")
    _st, body = await call_tool(off, headers, "take_message", {"reason": "off"}, rid=80)
    ref = body["result"]["structuredContent"]["ref"]
    calls, _ = cc_mod.ensure_agent(cc_app)
    assert next(r for r in calls.all() if r["ref"] == ref).get("conversation_id", "") == ""


async def test_conversation_id_matches_first_even_if_session_key_differs(stack):
    line, cc_app, cc_mod, _ = stack
    headers, _ = await init(line, call_id="conv-same")
    await call_tool(line, headers, "update_call", {"caller_name": "Pat Example"})
    # A second session for the same conversation (e.g. the agent reconnected).
    headers2, _ = await init(line, call_id="conv-same")
    await call_tool(line, headers2, "update_call", {"turn": {"speaker": "caller", "text": "Still me."}}, rid=4)
    calls, _ = cc_mod.ensure_agent(cc_app)
    rows = [r for r in calls.all() if r.get("conversation_id") == "conv-same"]
    assert len(rows) == 1 and rows[0]["turns"][0]["text"] == "Still me."


async def test_logs_hold_no_turn_or_summary_text(stack, caplog):
    caplog.set_level(logging.DEBUG)
    line, *_ = stack
    headers, _ = await init(line, call_id="conv-logs-turns")
    await call_tool(line, headers, "update_call", {"turn": {"speaker": "caller", "text": "secret-turn-words"},
                                                   "summary": "secret-summary-words"})
    assert "secret-turn-words" not in caplog.text and "secret-summary-words" not in caplog.text
    assert "conv-logs-turns" not in caplog.text


def test_tool_schema_has_turn_and_summary():
    import tools

    t = {x["name"]: x for x in tools.TOOLS}
    props = t["update_call"]["inputSchema"]["properties"]
    assert props["turn"]["properties"]["speaker"]["enum"] == ["caller", "agent"]
    assert props["turn"]["properties"]["text"]["maxLength"] == 500
    assert "summary" in t["take_message"]["inputSchema"]["properties"]
    assert "summary" in t["request_callback"]["inputSchema"]["properties"]
    assert "turn" not in t["take_message"]["inputSchema"]["properties"]
