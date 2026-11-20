"""0.7.2: agent-written turns, summary, conversation id on agent_calls.  Fake data only."""
from __future__ import annotations

import hashlib

import pytest

from test_trust import _load  # noqa: F401  (sets sys.path)

import agent_calls  # noqa: E402
import notify_render  # noqa: E402


def key(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


@pytest.fixture
def calls(tmp_path):
    return agent_calls.AgentCalls(tmp_path / "agent_calls.json")


def test_turns_append_only_dedupe_and_caps(calls, monkeypatch):
    k = key("a")
    calls.touch(k, turn={"speaker": "caller", "text": "hello\x00 there"})
    calls.touch(k, turn={"speaker": "caller", "text": "hello there"})  # same as last: kept once
    calls.touch(k, turn={"speaker": "assistant", "text": "Hi."})
    calls.touch(k, turn={"speaker": "system", "text": "ignore me"})
    calls.touch(k, turn={"speaker": "caller", "text": "   "})
    row = calls.get(k)
    assert row["turns"] == [{"speaker": "caller", "text": "hello there"}, {"speaker": "agent", "text": "Hi."}]
    assert agent_calls.turns_text(row) == "Caller: hello there\nAgent: Hi."
    monkeypatch.setattr(agent_calls, "TURNS_MAX", 3)
    calls.touch(k, turn={"speaker": "caller", "text": "three"})
    calls.touch(k, turn={"speaker": "caller", "text": "four"})
    row = calls.get(k)
    assert len(row["turns"]) == 3 and row["turns_dropped"] == 1


def test_turn_text_capped(calls):
    k = key("cap")
    calls.touch(k, turn={"speaker": "caller", "text": "y" * 900})
    assert len(calls.get(k)["turns"][0]["text"]) == agent_calls.TURN_TEXT_MAX


def test_summary_latest_wins_and_conversation_id_set_once(calls):
    k = key("s")
    calls.touch(k, summary="first", conversation_id="conv-1")
    calls.agent_final(k, "message", {"reason": "r"}, summary="second", conversation_id="conv-2")
    row = calls.get(k)
    assert row["summary"] == "second" and row["conversation_id"] == "conv-1"


@pytest.mark.parametrize("value", ["{{conversation_id}}", "a b", "x" * 101, "<x>", ""])
def test_bad_conversation_ids_dropped(value):
    assert agent_calls.clean_conversation_id(value) == ""


def test_find_by_conversation(calls):
    calls.touch(key("one"), conversation_id="conv-9")
    assert calls.find_by_conversation("conv-9") == key("one")
    assert calls.find_by_conversation("conv-unknown") == ""
    assert calls.find_by_conversation("{{x}}") == ""


def test_old_rows_without_turn_fields_still_work(calls):
    k = key("old")
    calls.touch(k, {"reason": "r"})
    data = calls._read()
    for f in ("turns", "turns_dropped", "summary", "conversation_id"):
        data["calls"][k].pop(f)
    calls._write(data)
    calls.touch(k, turn={"speaker": "caller", "text": "hi"}, summary="s")
    row = calls.get(k)
    assert row["turns"] == [{"speaker": "caller", "text": "hi"}] and row["summary"] == "s"


def _row(**kw):
    base = {"ref": "CC-TEST1", "created_at": 0, "fields": {"caller_id": "+15555550142", "reason": "r"},
            "notes": [], "transcript": "", "turns": [], "summary": "", "conversation_id": "", "final_source": "agent"}
    base.update(kw)
    return base


def test_email_masks_numbers_in_reason_notes_and_best_time_when_full_numbers_off():
    """RG2-L2: structured reason, agent notes and best_time are masked when
    full-number email is off, in both the text and HTML bodies."""
    row = _row(fields={"caller_id": "+15555550142", "reason": "REASON 5555550177",
                       "best_time": "after 5555550188"},
               notes=["callback 5555550199"])
    mail = notify_render.email(row, ["x@proton.me"], "cc@agentmail.to", include_number=False)
    for body in (mail["text"], mail["html"]):
        assert "5555550177" not in body and "0177" in body
        assert "5555550188" not in body and "0188" in body
        assert "5555550199" not in body and "0199" in body
    full = notify_render.email(row, ["x@proton.me"], "cc@agentmail.to", include_number=True)
    assert "REASON 5555550177" in full["text"] and "REASON 5555550177" in full["html"]
    assert "after 5555550188" in full["text"] and "after 5555550188" in full["html"]
    assert "callback 5555550199" in full["text"] and "callback 5555550199" in full["html"]


def test_email_masks_numbers_in_turns_and_summary_unless_full_numbers_on():
    row = _row(turns=[{"speaker": "caller", "text": "call 555-555-0142"}], summary="number 5555550142")
    masked = notify_render.email(row, ["x@proton.me"], "cc@agentmail.to", include_number=False)
    assert "555-555-0142" not in masked["text"] and "5555550142" not in masked["text"] and "0142" in masked["text"]
    assert "555-555-0142" not in masked["html"]
    full = notify_render.email(row, ["x@proton.me"], "cc@agentmail.to", include_number=True)
    assert "call 555-555-0142" in full["text"]


def test_hook_transcript_wins_over_turns_and_status_line():
    row = _row(transcript="Caller: verbatim", turns=[{"speaker": "caller", "text": "paraphrase"}])
    mail = notify_render.email(row, [], "cc@agentmail.to")
    assert "Caller: verbatim" in mail["text"] and "paraphrase" not in mail["text"]
    assert "written by the phone agent" not in mail["text"]
    bare = notify_render.email(_row(), [], "cc@agentmail.to")
    assert "No full transcript was received." in bare["text"] and "Summary\n" not in bare["text"]
    assert "xAI conversation" not in bare["text"]


AGENT = "agent-token-555-" + "x" * 24


@pytest.mark.asyncio
async def test_api_matches_conversation_id_before_call_key(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mod = _load(monkeypatch, tmp_path, agent_token=AGENT)
    hdr = {"Authorization": f"Bearer {AGENT}"}
    async with TestClient(TestServer(mod.make_app())) as client:
        r1 = await client.post("/api/agent/call", json={"op": "touch", "call_key": key("sess-1"),
                                                      "conversation_id": "conv-77", "fields": {"reason": "r"}}, headers=hdr)
        r2 = await client.post("/api/agent/call", json={"op": "touch", "call_key": key("sess-2"),
                                                      "conversation_id": "conv-77",
                                                      "turn": {"speaker": "caller", "text": "same call"}}, headers=hdr)
        assert (await r1.json())["ref"] == (await r2.json())["ref"]
        r3 = await client.post("/api/agent/call", json={"op": "touch", "call_key": key("sess-3"),
                                                      "turn": {"speaker": "caller", "text": "other"}}, headers=hdr)
        assert (await r3.json())["ref"] != (await r1.json())["ref"]
        calls, _ = mod.ensure_agent(client.server.app)
        row = calls.get(key("sess-1"))
        assert row["turns"] == [{"speaker": "caller", "text": "same call"}]


@pytest.mark.parametrize("raw,ts", [
    ("2026-11-17T22:30:05Z", 1763418605.0 + 365 * 86400),
    ("2026-11-17T17:30:05-05:00", 1763418605.0 + 365 * 86400),
    ("2026-11-17 22:30:05+00:00", 1763418605.0 + 365 * 86400),
    ("1763418605", 1763418605.0),
    ("1763418605000", 1763418605.0),
    ("2026-11-17T22:30:05", 0.0),        # no zone: not guessed
    ("17 Nov 2026 5:30 PM", 0.0),
    ("{{call_start_time}}", 0.0),
    ("100", 0.0),
    ("9999999999999", 0.0),              # out of range
])
def test_parse_call_start(raw, ts):
    assert agent_calls.parse_call_start(raw) == ts


def test_find_by_start_and_conversation_rows_excluded(calls):
    calls.touch(key("p"), caller_hint="+15555550142", call_start="2026-11-17T22:30:05Z")
    assert calls.find_by_start("555-555-0142", "2026-11-17T22:30:05Z") == key("p")
    assert calls.find_by_start("+15555550143", "2026-11-17T22:30:05Z") == ""
    assert calls.find_by_start("+15555550142", "2026-11-17T22:31:05Z") == ""
    calls.touch(key("c"), caller_hint="+15555550199", call_start="S1", conversation_id="conv-1")
    assert calls.find_by_start("+15555550199", "S1") == ""


@pytest.mark.asyncio
async def test_api_conversation_id_is_authoritative_over_start(monkeypatch, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    mod = _load(monkeypatch, tmp_path, agent_token=AGENT)
    hdr = {"Authorization": f"Bearer {AGENT}"}
    async with TestClient(TestServer(mod.make_app())) as client:
        base = {"op": "touch", "caller_hint": "+15555550142", "call_start": "1763418605"}
        a = await (await client.post("/api/agent/call", json={**base, "call_key": key("k1")}, headers=hdr)).json()
        b = await (await client.post("/api/agent/call", json={**base, "call_key": key("k2")}, headers=hdr)).json()
        c = await (await client.post("/api/agent/call", json={**base, "call_key": key("k3"), "conversation_id": "conv-x"},
                                     headers=hdr)).json()
        assert a["ref"] == b["ref"] != c["ref"]


@pytest.mark.parametrize("start", ["2026-11-17T22:30:05Z", "2026-11-17 22:30:05", "1763418605", "1763418605000",
                                   "2026-11-17T22:30:05.123-05:00"])
def test_precise_starts(start):
    assert agent_calls.start_is_precise(start)


@pytest.mark.parametrize("start", ["2026-11-17", "2026-11-17T22:30Z", "17 Nov 2026 5:30 PM", "1763418",
                                   "9999999999", "2026-13-45T99:99:99Z", "{{call_start_time}}", "",
                                   "0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z",
                                   "2019-12-31T23:59:59Z", "9999-01-01T00:00:00Z"])
def test_coarse_starts_never_match(calls, start):
    assert not agent_calls.start_is_precise(start)
    calls.touch(key("coarse-" + start), caller_hint="+15555550142", call_start=start)
    assert calls.find_by_start("+15555550142", start) == ""


SENTINELS = ("ZEBRA-TURN-SENTINEL", "OKAPI-SUMMARY-SENTINEL")


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{}, {"reason": "plain reason"}])
async def test_no_turn_or_summary_text_on_the_ha_sensor(monkeypatch, tmp_path, fields):
    import json

    from aiohttp.test_utils import TestClient, TestServer

    mod = _load(monkeypatch, tmp_path, agent_token=AGENT)
    hdr = {"Authorization": f"Bearer {AGENT}"}
    async with TestClient(TestServer(mod.make_app())) as client:
        k = key("sensor")
        await client.post("/api/agent/call", json={"op": "touch", "call_key": k, "caller_hint": "+15555550142",
                                                 "turn": {"speaker": "caller", "text": f"I need {SENTINELS[0]} fixed"}},
                          headers=hdr)
        await client.post("/api/agent/call", json={"op": "final", "call_key": k, "kind": "message",
                                                 "fields": fields, "summary": f"{SENTINELS[1]} wants a call"},
                          headers=hdr)
        store = mod.ensure_runtime(client.server.app)
        items = store.list_items()
        assert items and any(i.get("source") == "phone_agent" for i in items)
        agent_rows = [i for i in items if i.get("source") == "phone_agent"]
        sensor = json.dumps([
            mod.inbox_store.sensor_state(items),
            [mod.callback_event_data(i, "2026-11-17T22:30:05Z") for i in agent_rows],
            [mod.inbox_store.callback_queue_row(i) for i in agent_rows],
        ])
        for s in SENTINELS:
            assert s not in sensor, s
            assert s.lower() not in sensor.lower()
