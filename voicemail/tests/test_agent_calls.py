"""0.7.0 phone agent: record before hangup, exactly-once notify, retry, busy-only.

Fake data only: 555-01xx numbers, invented names, invented events.
"""
from __future__ import annotations

import hashlib
import json
import logging
import pathlib

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load

import agent_calls  # noqa: E402  (path set by test_trust)
import notify_outbox  # noqa: E402

AGENT = "agent-token-555-" + "x" * 24
INGEST = "ingest-token-555"
CALLER = "+15555550142"
HDR = {"Authorization": f"Bearer {AGENT}"}


async def _helper_off(_app, _entity):
    return "off"


def key(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:32]


@pytest.fixture
def mod(monkeypatch, tmp_path):
    return _load(monkeypatch, tmp_path, agent_token=AGENT, push_service="mobile_app_test_phone",
                 mail_to="callcatch-test@proton.me", agentmail_inbox="callcatch-test@agentmail.to")


def _jobs(mod, app):
    _calls, outbox = mod.ensure_agent(app)
    return json.loads(outbox.path.read_text())["jobs"] if outbox.path.exists() else {}


async def _call(client, op, k, **extra):
    resp = await client.post("/api/agent/call", json={"op": op, "call_key": k, **extra}, headers=HDR)
    return resp.status, await resp.json()


# --- auth -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_routes_need_their_own_bearer(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        body = {"op": "touch", "call_key": key("a")}
        assert (await client.post("/api/agent/call", json=body)).status == 401
        bad = await client.post("/api/agent/call", json=body, headers={"Authorization": f"Bearer {INGEST}"})
        assert bad.status == 401
        assert (await client.get("/api/agent/freebusy", headers={"Authorization": "Bearer nope"})).status == 401
        ok = await client.post("/api/agent/call", json=body, headers=HDR)
        assert ok.status == 200
        # The agent token does not open sensitive reads.
        assert (await client.get("/api/voicemails", headers=HDR)).status in (401, 403)


@pytest.mark.asyncio
async def test_agent_token_equal_to_ingest_or_short_disables(monkeypatch, tmp_path):
    for token in (INGEST, "short-token"):
        m = _load(monkeypatch, tmp_path, agent_token=token)
        assert m.AGENT_TOKEN == ""
        async with TestClient(TestServer(m.make_app())) as client:
            r = await client.post("/api/agent/call", json={"op": "touch", "call_key": key("a")},
                                  headers={"Authorization": f"Bearer {token}"})
            assert r.status == 503


@pytest.mark.asyncio
async def test_oversize_and_bad_bodies_rejected(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        big = {"op": "touch", "call_key": key("a"), "fields": {"notes": "x" * (100 * 1024)}}
        assert (await client.post("/api/agent/call", json=big, headers=HDR)).status == 400
        assert (await client.post("/api/agent/call", data="not json", headers=HDR)).status == 400
        assert (await client.post("/api/agent/call", json={"op": "drop_table", "call_key": key("a")}, headers=HDR)).status == 400
        assert (await client.post("/api/agent/call", json={"op": "touch", "call_key": "../etc"}, headers=HDR)).status == 400


# --- record before hangup + finalizer ---------------------------------------

@pytest.mark.asyncio
async def test_hangup_before_take_message_still_notifies_once(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        k = key("early")
        st, body = await _call(client, "touch", k, caller_hint=CALLER,
                               fields={"caller_name": "Pat Testcaller", "reason": "about the fence quote"})
        assert st == 200 and set(body) == {"ok", "ref", "state"} and body["state"] == "open"
        ref = body["ref"]
        calls, _ = mod.ensure_agent(app)
        assert calls.get(k)["fields"]["caller_name"] == "Pat Testcaller"   # record exists before hangup
        assert _jobs(mod, app) == {}

        # Caller hangs up; no take_message.  Finalizer runs after the quiet window.
        row = calls.get(k)
        done = calls.sweep(180, False, 120, now=row["last_activity"] + 181)
        assert len(done) == 1 and done[0]["hung_up_early"] and done[0]["final_source"] == "timeout"
        for r in done:
            await mod.deliver_final(app, r)
        jobs = _jobs(mod, app)
        assert set(jobs) == {f"email:{ref}", f"push:{ref}"}
        mail = jobs[f"email:{ref}"]["payload"]
        assert "Caller hung up early." in mail["text"] and "(hung up early)" in mail["subject"]
        assert "about the fence quote" in mail["text"]
        assert mail["to"] == ["callcatch-test@proton.me"] and mail["from"] == "callcatch-test@agentmail.to"
        push = jobs[f"push:{ref}"]["payload"]
        assert push["title"] == "CallCatch: Pat Testcaller"
        assert ref in push["message"] and "fence" not in json.dumps(push) and "0142" not in json.dumps(push)
        item = mod.ensure_runtime(app).get("pa-" + ref.lower())
        assert item["source"] == "phone_agent" and item["hung_up_early"] is True

        # A second sweep and a late take_message do not send the email again.
        assert calls.sweep(180, False, 120, now=row["last_activity"] + 999) == []
        st, body = await _call(client, "final", k, kind="message", fields={"notes": "wants a call Tuesday"})
        assert st == 200 and body["state"] == "final"
        jobs = _jobs(mod, app)
        assert set(jobs) == {f"email:{ref}", f"push:{ref}", f"push_update:{ref}"}
        assert "Tuesday" in mod.ensure_runtime(app).get("pa-" + ref.lower())["transcript"]


@pytest.mark.asyncio
async def test_agent_final_sends_immediately_without_hook(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        k = key("normal")
        await _call(client, "touch", k, caller_hint=CALLER)
        st, body = await _call(client, "final", k, kind="callback",
                               fields={"caller_name": "Lee Example", "callback_number": "555-555-0143",
                                       "reason": "invoice question", "urgency": "urgent"})
        assert st == 200 and body["state"] == "final"
        ref = body["ref"]
        jobs = _jobs(mod, app)
        assert set(jobs) == {f"email:{ref}", f"push:{ref}"}
        assert "Callback requested." in jobs[f"push:{ref}"]["payload"]["message"]
        text = jobs[f"email:{ref}"]["payload"]["text"]
        assert "•••0143" in text and "555-0143" not in text and "Caller hung up early" not in text  # M1 default
        item = mod.ensure_runtime(app).get("pa-" + ref.lower())
        assert item["callback_status"] == "pending" and item["last_actor"] == "agent"
        # Duplicate final: nothing new.
        await _call(client, "final", k, kind="callback", fields={"caller_name": "Lee Example"})
        assert set(_jobs(mod, app)) == {f"email:{ref}", f"push:{ref}"}


# --- webhook: early, late, duplicate ----------------------------------------

@pytest.mark.asyncio
async def test_webhook_finalizes_then_finalizer_and_duplicate_do_nothing(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        k = key("hooked")
        _, body = await _call(client, "touch", k, caller_hint=CALLER, fields={"caller_name": "Sam Sample"})
        ref = body["ref"]
        hook = {"call_key": k, "hook_id": "evt_1", "caller_hint": CALLER,
                "transcript": [{"role": "assistant", "text": "Hi, this is the agent."},
                               {"role": "user", "text": "Hi, it is Sam, I will call back."}]}
        r = await client.post("/api/agent/hook", json=hook, headers=HDR)
        assert r.status == 200 and (await r.json())["state"] == "final"
        mail = _jobs(mod, app)[f"email:{ref}"]["payload"]
        assert "Caller: Hi, it is Sam" in mail["text"] and "Caller hung up early." in mail["text"]
        calls, _ = mod.ensure_agent(app)
        assert calls.sweep(180, False, 120, now=agent_calls.now_ts() + 9999) == []
        dup = await client.post("/api/agent/hook", json=hook, headers=HDR)
        assert (await dup.json())["duplicate"] is True
        assert set(_jobs(mod, app)) == {f"email:{ref}", f"push:{ref}"}


@pytest.mark.asyncio
async def test_late_webhook_after_finalizer_attaches_transcript_no_resend(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        k = key("late-hook")
        _, body = await _call(client, "touch", k, caller_hint=CALLER, fields={"caller_name": "Ana Fakename"})
        ref = body["ref"]
        calls, _ = mod.ensure_agent(app)
        for r in calls.sweep(180, False, 120, now=agent_calls.now_ts() + 200):
            await mod.deliver_final(app, r)
        hook = {"call_key": k, "hook_id": "evt_late", "transcript": "Caller: sorry, bad signal."}
        for _ in range(3):  # late and duplicated
            assert (await client.post("/api/agent/hook", json=hook, headers=HDR)).status == 200
        jobs = _jobs(mod, app)
        assert set(jobs) == {f"email:{ref}", f"push:{ref}", f"push_update:{ref}"}
        assert "bad signal" in mod.ensure_runtime(app).get("pa-" + ref.lower())["transcript"]


@pytest.mark.asyncio
async def test_hook_expected_waits_for_transcript_then_grace(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, hook_expected=True, hook_grace_s=120)
    async with TestClient(TestServer(m.make_app())) as client:
        app = client.server.app
        calls, _ = m.ensure_agent(app)
        # A: agent final waits, hook arrives, one email with transcript.
        ka = key("wait-a")
        _, b = await _call(client, "final", ka, kind="message", fields={"caller_name": "Kim Demo", "reason": "gutter"})
        assert b["state"] == "awaiting_hook" and _jobs(m, app) == {}
        await client.post("/api/agent/hook", json={"call_key": ka, "hook_id": "e1", "transcript": "Caller: gutter repair."}, headers=HDR)
        mail = _jobs(m, app)[f"email:{b['ref']}"]["payload"]
        assert "gutter repair" in mail["text"] and "hung up early" not in mail["text"].lower()
        # B: no hook ever comes; grace expires; one email from notes, not early.
        kb = key("wait-b")
        _, b2 = await _call(client, "final", kb, kind="message", fields={"caller_name": "Jo Demo", "notes": "left a note"})
        for r in calls.sweep(180, True, 120, now=agent_calls.now_ts() + 121):
            await m.deliver_final(app, r)
        mail2 = _jobs(m, app)[f"email:{b2['ref']}"]["payload"]
        assert "left a note" in mail2["text"] and "No full transcript was received." in mail2["text"]
        assert "Caller hung up early" not in mail2["text"]


@pytest.mark.asyncio
async def test_hook_with_no_prior_session_creates_and_notifies(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        k = key("hook-only")
        r = await client.post("/api/agent/hook", json={"call_key": k, "hook_id": "e9", "caller_hint": CALLER,
                                                        "transcript": "Caller: hello?"}, headers=HDR)
        body = await r.json()
        assert body["state"] == "final"
        assert f"email:{body['ref']}" in _jobs(mod, app)


@pytest.mark.asyncio
async def test_hook_without_key_matches_open_call_by_caller(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        k = key("match")
        _, b = await _call(client, "touch", k, caller_hint=CALLER)
        r = await client.post("/api/agent/hook", json={"hook_id": "e7", "caller_hint": "555-555-0142",
                                                        "transcript": "Caller: matched."}, headers=HDR)
        assert (await r.json())["ref"] == b["ref"]


# --- outbox retry ----------------------------------------------------------

@pytest.mark.asyncio
async def test_mail_failure_then_retry_sends_once(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "o.json")
    assert ob.enqueue("email", "CC-ACDEF", {"to": ["a@example.com"]}) is True
    assert ob.enqueue("email", "CC-ACDEF", {"to": ["a@example.com"]}) is False
    calls = []
    outcomes = [(False, "http-503", True), (False, "timeouterror", True), (True, "sent", False)]

    async def flaky(job):
        calls.append(job["id"])
        return outcomes[len(calls) - 1]

    t = notify_outbox.now_ts() + 1
    r1 = await ob.pump_once({"email": flaky}, clock=t)
    assert r1[0]["status"] == "pending" and r1[0]["attempts"] == 1
    assert await ob.pump_once({"email": flaky}, clock=t + 5) == []  # backoff holds it
    r2 = await ob.pump_once({"email": flaky}, clock=t + notify_outbox.backoff(1))
    assert r2[0]["attempts"] == 2
    r3 = await ob.pump_once({"email": flaky}, clock=t + 10_000)
    assert r3[0]["status"] == "sent" and r3[0]["payload"] == {}
    assert await ob.pump_once({"email": flaky}, clock=t + 99_999) == []
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_permanent_failure_parks_dead_and_missing_sender_waits(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "o.json")
    ob.enqueue("email", "CC-ACDEG", {})
    ob.enqueue("push", "CC-ACDEG", {})

    async def reject(_job):
        return False, "http-400", False

    await ob.pump_once({"email": reject}, clock=notify_outbox.now_ts() + 1)
    assert ob.get("email:CC-ACDEG")["status"] == "dead"
    job = ob.get("push:CC-ACDEG")
    assert job["status"] == "pending" and job["attempts"] == 0
    assert ob.counts() == {"pending": 1, "sent": 0, "dead": 1, "dry_run": 0, "expired": 0}


@pytest.mark.asyncio
async def test_sender_exception_is_retried(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "o.json")
    ob.enqueue("push", "CC-ACDEH", {})

    async def boom(_job):
        raise RuntimeError("secret-ish detail")

    rec = (await ob.pump_once({"push": boom}, clock=notify_outbox.now_ts() + 1))[0]
    assert rec["status"] == "pending" and rec["last_code"] == "runtimeerror"


# --- privacy ---------------------------------------------------------------

def test_gmail_is_never_a_mail_address():
    import notify_send

    assert not notify_send.mail_allowed("a@sub.gmail.com")
    assert notify_send.parse_recipient("someone@gmail.com") == ""
    assert notify_send.parse_recipient("x@googlemail.com") == ""


def test_recipient_pinned_to_one_proton_address():
    """G1: one recipient, on a Proton domain pinned in code."""
    import notify_send

    assert notify_send.RECIPIENT_DOMAINS == ("proton.me", "protonmail.com")
    assert notify_send.parse_recipient("callcatch-test@proton.me") == "callcatch-test@proton.me"
    assert notify_send.parse_recipient("callcatch-test@protonmail.com") == "callcatch-test@protonmail.com"
    for bad in ("someone@example.com", "x@evil.proton.me", "x@proton.me.evil.com", "x@pm.me",
                "a@proton.me, b@proton.me", "a@proton.me;b@proton.me", "a@proton.me\nBcc: c@evil.com", ""):
        assert notify_send.parse_recipient(bad) == "", bad
    assert notify_send.sender_allowed("callcatch-test@agentmail.to")
    for bad in ("someone@mail.grokbot.example", "x@gmail.com", "x@agentmail.to.evil.com"):
        assert not notify_send.sender_allowed(bad), bad


@pytest.mark.asyncio
async def test_gmail_payload_refused_by_sender():
    import notify_send

    hits = []

    class S:
        def post(self, *a, **k):
            hits.append(1)
            raise AssertionError("must not post")

    send = notify_send.agentmail_sender(lambda: S(), "callcatch-test@agentmail.to", "k" * 40, "x@gmail.com")
    assert await send({"payload": {"to": ["x@gmail.com"]}}) == (False, "blocked-address", False)
    send = notify_send.agentmail_sender(lambda: S(), "callcatch-test@agentmail.to", "k" * 40, "callcatch-test@proton.me")
    for payload in (
        {"to": ["x@gmail.com"], "subject": "s", "text": "t", "html": "h", "category": "callcatch_phone_message"},
        {"to": ["callcatch-test@proton.me", "other@proton.me"], "subject": "s", "text": "t", "html": "h",
         "category": "callcatch_phone_message"},
        {"to": ["callcatch-test@proton.me"], "subject": "s", "text": "t", "html": "h", "category": "other"},
        {"to": ["callcatch-test@proton.me"], "subject": "s\r\nBcc: x@evil.com", "text": "t", "html": "h",
         "category": "callcatch_phone_message"},
    ):
        assert await send({"payload": payload}) == (False, "blocked-payload", False)
    assert hits == []


def test_mail_options_refuse_gmail(monkeypatch, tmp_path):
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="agentmail", mail_to="me@gmail.com",
              agentmail_inbox="me@gmail.com", agentmail_api_key="k" * 40)
    assert m.MAIL_TO == "" and m.MAIL_FROM == "" and m.mail_ready() is False
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="grok_outbox", mail_to="callcatch-test@proton.me",
              agentmail_inbox="callcatch-test@agentmail.to", agentmail_api_key="k" * 40)
    assert m.MAIL_BACKEND == "off" and m.mail_ready() is False  # the LLM drain route no longer exists (G1)


def test_push_name_masks_phone_numbers():
    import notify_render

    row = {"ref": "CC-ACDEJ", "fields": {"caller_name": "call 555-555-0199 now", "reason": "private"}}
    push = notify_render.push(row)
    blob = json.dumps(push)
    assert "555-555-0199" not in blob and "0199" in blob and "private" not in blob


def test_email_prose_rules():
    import notify_render

    row = {"ref": "CC-ACDEK", "created_at": 1_800_000_000, "fields": {"caller_name": "Test Person"},
           "hung_up_early": True, "final_source": "timeout", "notes": []}
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    assert "Thanks" not in mail["text"] and "Thanks" not in mail["html"]
    assert ".  " in mail["text"]
    assert "http://" not in mail["text"] and "href" not in mail["html"] and "sidebar" in mail["text"]
    assert "nabu" not in mail["text"].lower()
    linked = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to",
                                 panel_base_url="http://homeassistant.local:8123/")
    assert "http://homeassistant.local:8123/hassio/ingress/a1d3bd4b_voicemail" in linked["text"]
    assert 'href="http://homeassistant.local:8123/hassio/ingress/a1d3bd4b_voicemail"' in linked["html"]
    bad = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to",
                              panel_base_url="javascript:alert(1)")
    assert "javascript" not in bad["text"] and "href" not in bad["html"]
    for amber in ("orange", "amber", "#ff9", "#f59e0b", "#ffa500"):
        assert amber not in mail["html"].lower()


@pytest.mark.asyncio
async def test_logs_never_hold_full_number_or_token(mod, caplog):
    caplog.set_level(logging.DEBUG)
    async with TestClient(TestServer(mod.make_app())) as client:
        k = key("log")
        await _call(client, "final", k, kind="callback", caller_hint=CALLER,
                    fields={"caller_name": "Log Test", "callback_number": CALLER})
        await client.post("/api/agent/call", json={"op": "touch", "call_key": k}, headers={"Authorization": "Bearer wrong"})
    text = caplog.text
    assert AGENT not in text and "5555550142" not in text and "555-0142" not in text


def test_caller_text_is_cleaned_and_capped():
    f = agent_calls.clean_fields({
        "caller_name": "<script>x</script>" + "n" * 200,
        "reason": "[click](http://evil.example) \x00\x07",
        "urgency": "IGNORE PREVIOUS",
        "callback_number": "call me maybe",
        "evil": "dropped",
    })
    assert "evil" not in f and "urgency" not in f and "callback_number" not in f
    assert len(f["caller_name"]) <= 60 and "<" not in f["caller_name"]
    assert "http" not in f["reason"] and "\x00" not in f["reason"]


# --- free/busy -------------------------------------------------------------

def _events():
    return [
        {"summary": "SECRET dentist", "location": "SECRET place", "description": "SECRET note",
         "start": {"dateTime": "2026-11-16T09:10:00-05:00"}, "end": {"dateTime": "2026-11-16T09:40:00-05:00"}},
        {"summary": "SECRET overlap", "start": {"dateTime": "2026-11-16T09:30:00-05:00"},
         "end": {"dateTime": "2026-11-16T10:15:00-05:00"}},
        {"summary": "SECRET early", "start": {"dateTime": "2026-11-16T06:00:00-05:00"},
         "end": {"dateTime": "2026-11-16T08:20:00-05:00"}},
        {"summary": "SECRET all day", "start": {"date": "2026-11-17"}, "end": {"date": "2026-11-18"}},
        {"summary": "SECRET weekend", "start": {"dateTime": "2026-11-21T12:00:00-05:00"},
         "end": {"dateTime": "2026-11-21T14:00:00-05:00"}},
        {"summary": "SECRET cancelled", "status": "cancelled", "start": {"dateTime": "2026-11-18T12:00:00-05:00"},
         "end": {"dateTime": "2026-11-18T13:00:00-05:00"}},
    ]


def test_busy_blocks_merge_round_and_redact():
    import freebusy
    from datetime import date

    blocks = freebusy.busy_blocks(_events(), date(2026, 11, 16), 7, freebusy.parse_window("08:00-18:00"))
    assert blocks == [
        {"start": "2026-11-16T08:00-05:00", "end": "2026-11-16T08:30-05:00"},
        {"start": "2026-11-16T09:00-05:00", "end": "2026-11-16T10:30-05:00"},
    ]
    assert "SECRET" not in json.dumps(freebusy.response(blocks, date(2026, 11, 16), 7, (freebusy.parse_window("")) ))


def test_busy_range_rules():
    import freebusy
    from datetime import date

    today = date(2026, 11, 16)
    assert freebusy.validate_range("2026-11-15", 5, today) == "past-date"
    assert freebusy.validate_range("2026-11-16", 15, today) == "bad-days"
    assert freebusy.validate_range("2027-03-01", 5, today) == "too-far-ahead"
    assert freebusy.validate_range("2026-12-01", 1, today) == "too-far-ahead"  # 14 days max ahead (I6)
    assert freebusy.validate_range("2026-11-30", 1, today) == (date(2026, 11, 30), 1)
    assert freebusy.validate_range("16/11/2026", 5, today) == "bad-start-date"
    assert freebusy.validate_range("", "", today) == (today, 5)
    assert freebusy.parse_calendars("calendar.home, calendar.shared,calendar.kids;drop table") == ["calendar.home", "calendar.shared"]


@pytest.mark.asyncio
async def test_freebusy_route_reads_only_configured_calendars(mod):
    seen = []

    async def fake_events(_session, entity, _s, _e):
        seen.append(entity)
        return _events()

    mod.ha_calendar_events = fake_events
    async with TestClient(TestServer(mod.make_app())) as client:
        r = await client.get("/api/agent/freebusy", params={"days": "3"}, headers=HDR)
        body = await r.json()
        assert r.status == 200 and body["ok"] is True
        assert "SECRET" not in json.dumps(body)
        assert set(body) == {"ok", "tz", "from", "days", "window", "busy", "note"}
        assert seen == ["calendar.home", "calendar.shared"]
        assert (await client.get("/api/agent/freebusy", params={"days": "30"}, headers=HDR)).status == 400


class _Resp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _Sess:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []
        self.kwargs = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append((url, json, headers))
        self.kwargs.append(kw)
        return _Resp(self.statuses.pop(0))


@pytest.mark.asyncio
async def test_agentmail_sender_is_direct_verbatim_and_classified():
    """G1: one HTTPS POST to api.agentmail.to, rendered content verbatim, fixed labels."""
    import notify_render
    import notify_send

    row = {"ref": "CC-ACDEL", "created_at": 1_800_000_000, "fields": {"caller_name": "Verbatim Test", "reason": "gutters"},
           "notes": ["back Tuesday"], "transcript": "Caller: hello."}
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    sess = _Sess([503, 200, 403, 429])
    send = notify_send.agentmail_sender(lambda: sess, "callcatch-test@agentmail.to", "k" * 40, "callcatch-test@proton.me")
    job = {"payload": mail}
    assert await send(job) == (False, "http-503", True)
    assert await send(job) == (True, "sent", False)
    assert await send(job) == (False, "http-403", False)   # e.g. a recipient suppression: parks dead, visible in diag
    assert await send(job) == (False, "http-429", True)
    url, body, headers = sess.calls[1]
    assert url == "https://api.agentmail.to/v0/inboxes/callcatch-test@agentmail.to/messages/send"
    assert body == {"to": ["callcatch-test@proton.me"], "subject": mail["subject"], "text": mail["text"],
                    "html": mail["html"], "labels": ["callcatch", "callcatch_phone_message"]}
    assert headers["Authorization"] == "Bearer " + "k" * 40
    assert sess.kwargs[1]["allow_redirects"] is False


@pytest.mark.asyncio
async def test_mail_route_end_to_end_and_key_never_logged(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    key_value = "agentmail-test-key-" + "z" * 30
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="agentmail", mail_to="callcatch-test@proton.me",
              agentmail_inbox="callcatch-test@agentmail.to", agentmail_api_key=key_value)
    assert m.mail_ready() and m.MAIL_FROM == "callcatch-test@agentmail.to"
    sess = _Sess([200, 200])
    m.read_entity_state = _helper_off
    async with TestClient(TestServer(m.make_app())) as client:
        app = client.server.app
        box = m._box(app)
        real = box.get("session")
        box["session"] = sess                      # the real build_senders, pointed at a fake session
        box["senders"] = m.build_senders(app)
        try:
            _, b = await _call(client, "final", key("e2e"), kind="message", caller_hint=CALLER,
                               fields={"caller_name": "E2E Test", "reason": "roof"})
            await m.agent_tick(app)
            _calls, outbox = m.ensure_agent(app)
            assert outbox.get(f"email:{b['ref']}")["status"] == "sent"
        finally:
            box["session"] = real
    assert len(sess.calls) == 1 and sess.calls[0][1]["to"] == ["callcatch-test@proton.me"]
    assert sess.calls[0][0].startswith("https://api.agentmail.to/")
    assert key_value not in caplog.text


@pytest.mark.asyncio
async def test_push_sender_uses_notify_service_only():
    import notify_send

    seen = []

    async def call_service(domain, service, data):
        seen.append((domain, service, data))
        return 200, {}

    send = notify_send.push_sender(call_service, "mobile_app_test_phone")
    assert (await send({"payload": {"title": "CallCatch: X", "message": "New phone message CC-ACDEM."}}))[0]
    assert seen[0][0] == "notify" and seen[0][1] == "mobile_app_test_phone"
    bad = notify_send.push_sender(call_service, "persistent_notification")
    assert await bad({"payload": {}}) == (False, "not-configured", True)


@pytest.mark.asyncio
async def test_agent_tick_runs_finalizer_and_pump(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake_push(job):
            sent.append(job["id"])
            return True, "sent", False

        mod._box(app)["senders"] = {"push": fake_push}
        mod.read_entity_state = _helper_off
        k = key("tick")
        _, b = await _call(client, "touch", k, fields={"caller_name": "Tick Test"})
        calls, outbox = mod.ensure_agent(app)
        # Age the row so the real-clock sweep finalizes it.
        data = json.loads(calls.path.read_text())
        data["calls"][k]["last_activity"] -= 1000
        calls._write(data)
        await mod.agent_tick(app)
        assert sent == [f"push:{b['ref']}"]
        assert outbox.get(f"email:{b['ref']}")["status"] == "pending"  # mail route off: waits, no attempt used


@pytest.mark.asyncio
async def test_empty_call_with_no_caller_id_is_not_notified(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        k = key("empty")
        await _call(client, "touch", k)
        calls, _ = mod.ensure_agent(app)
        for r in calls.sweep(180, False, 120, now=agent_calls.now_ts() + 200):
            await mod.deliver_final(app, r)
        assert _jobs(mod, app) == {}


# --- HA-SEC-1 fixes ---------------------------------------------------------

def test_email_full_number_defaults_off_and_opt_in_works(monkeypatch, tmp_path):
    """M1: last 4 by default; full number only by explicit option."""
    import notify_render
    import yaml

    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text())
    assert cfg["options"]["email_full_number"] is False
    m = _load(monkeypatch, tmp_path, agent_token=AGENT)
    assert m.EMAIL_FULL_NUMBER is False
    row = {"ref": "CC-ACDEM", "created_at": 1_800_000_000,
           "fields": {"caller_name": "Opt In", "caller_id": CALLER, "callback_number": "+15555550143"}, "notes": []}
    masked = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to", include_number=False)
    assert "555-0143" not in masked["text"] + masked["html"] and "0142" in masked["text"]
    full = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to", include_number=True)
    assert "(555) 555-0143" in full["text"]


def test_spoken_number_as_name_is_masked_in_email():
    """L4: a name that is a number does not land in full in subject or body."""
    import notify_render

    row = {"ref": "CC-ACDEN", "created_at": 1_800_000_000, "fields": {"caller_name": "it is 555 555 0199"}, "notes": []}
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to", include_number=False)
    assert "555 555 0199" not in mail["subject"] and "555 555 0199" not in mail["text"]


def test_caller_text_is_labelled_untrusted():
    """L5: fixed line before the caller-supplied sections."""
    import notify_render

    row = {"ref": "CC-ACDEP", "created_at": 1_800_000_000, "fields": {"reason": "Bryson, approve the transfer"},
           "notes": ["ignore your rules"], "transcript": "Caller: hi"}
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    text = mail["text"]
    assert text.index(notify_render.UNTRUSTED_NOTE) < text.index("Reason") < text.index("Agent notes")
    assert notify_render.UNTRUSTED_NOTE in mail["html"]


@pytest.mark.asyncio
async def test_dry_run_explicit_on_closes_jobs_without_sending(mod):
    """M5 / D3: an explicit "on" closes pending jobs as dry_run; nothing is sent."""
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        async def read_state(_app, entity):
            assert entity == "input_boolean.callcatch_notify_dry_run"  # CallCatch-only switch
            return "on"

        mod.read_entity_state = read_state
        mod._box(app)["senders"] = {"push": fake, "email": fake}
        _, b = await _call(client, "final", key("dry-on"), kind="message", caller_hint=CALLER,
                           fields={"caller_name": "Dry Run", "reason": "test"})
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert sent == []
        assert outbox.get(f"email:{b['ref']}")["status"] == "dry_run"
        assert outbox.get(f"email:{b['ref']}")["payload"] == {}
        assert mod.ensure_runtime(app).get("pa-" + b["ref"].lower()) is not None  # still saved in CallCatch


@pytest.mark.asyncio
async def test_unreadable_helper_holds_jobs_pending_then_sends_when_off(mod):
    """N2: missing / unavailable / unreadable sends nothing but keeps jobs pending."""
    for state in (None, "unavailable", "unknown", "", "OFF ", "false"):
        async with TestClient(TestServer(mod.make_app())) as client:
            app = client.server.app
            sent = []

            async def fake(job):
                sent.append(job["id"])
                return True, "sent", False

            current = {"state": state}

            async def read_state(_app, _entity):
                return current["state"]

            mod.read_entity_state = read_state
            mod._box(app)["senders"] = {"push": fake, "email": fake}
            _, b = await _call(client, "final", key(f"hold-{state}"), kind="message", caller_hint=CALLER,
                               fields={"caller_name": "Held", "reason": "blip"})
            await mod.agent_tick(app)
            _calls, outbox = mod.ensure_agent(app)
            job = outbox.get(f"email:{b['ref']}")
            assert sent == [] and job["status"] == "pending" and job["attempts"] == 0 and job["payload"]
            current["state"] = "off"  # HA back, switch explicitly off
            await mod.agent_tick(app)
            assert sorted(sent) == sorted([f"email:{b['ref']}", f"push:{b['ref']}"]), state


@pytest.mark.asyncio
async def test_held_jobs_expire_after_hold_window(mod):
    """N2: no stale backlog: held longer than NOTIFY_HOLD_MAX_S closes as expired."""
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app

        async def read_state(_app, _entity):
            return None

        mod.read_entity_state = read_state
        mod._box(app)["senders"] = {}
        _, b = await _call(client, "final", key("expire"), kind="message", fields={"caller_name": "Old", "reason": "x"})
        _calls, outbox = mod.ensure_agent(app)
        data = json.loads(outbox.path.read_text())
        for job in data["jobs"].values():
            job["created_at"] -= mod.NOTIFY_HOLD_MAX_S + 5
        outbox._write(data)
        await mod.agent_tick(app)
        assert outbox.get(f"email:{b['ref']}")["status"] == "expired"
        assert outbox.counts()["expired"] == 2


@pytest.mark.asyncio
async def test_dry_run_off_sends_once(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        async def read_state(_app, entity):
            # work_forward_dry_run is not consulted: off there would not matter.
            return "off" if entity == "input_boolean.callcatch_notify_dry_run" else "on"

        mod.read_entity_state = read_state
        mod._box(app)["senders"] = {"push": fake, "email": fake}
        _, b = await _call(client, "final", key("dry-off"), kind="message", fields={"caller_name": "Live", "reason": "x"})
        await mod.agent_tick(app)
        await mod.agent_tick(app)
        assert sorted(sent) == sorted([f"email:{b['ref']}", f"push:{b['ref']}"])


@pytest.mark.asyncio
async def test_chunked_agent_body_is_read_whole(mod):
    """L1 on the CallCatch side."""
    import asyncio

    raw = json.dumps({"op": "touch", "call_key": key("chunk"), "fields": {"caller_name": "Chunky"}}).encode()

    async def split():
        yield raw[:15]
        await asyncio.sleep(0.02)
        yield raw[15:]

    async def oversize():
        yield raw[:-1]
        await asyncio.sleep(0.02)
        for _ in range(100):
            yield b" " * 1024

    async with TestClient(TestServer(mod.make_app())) as client:
        r = await client.post("/api/agent/call", data=split(), headers={**HDR, "Content-Type": "application/json"})
        assert r.status == 200
        r = await client.post("/api/agent/call", data=oversize(), headers={**HDR, "Content-Type": "application/json"})
        assert r.status == 400


@pytest.mark.asyncio
async def test_agent_callback_fires_no_extra_event(mod):
    """L3: the outbox push is the one owner of the phone-agent callback notice."""
    fired = []

    async def fake_fire(_session, event_type, data):
        fired.append(event_type)
        return 200, {}

    mod.ha_fire_event = fake_fire
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        _, b = await _call(client, "final", key("cb-event"), kind="callback", caller_hint=CALLER,
                           fields={"caller_name": "Call Me", "reason": "x"})
        item = mod.ensure_runtime(app).get("pa-" + b["ref"].lower())
        assert item["callback_status"] == "pending"
        assert fired == [] and f"push:{b['ref']}" in _jobs(mod, app)


@pytest.mark.asyncio
async def test_timeout_notices_capped_per_day(monkeypatch, tmp_path):
    """L7: hung-up-early notices are capped per day; calls are still saved."""
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, timeout_notify_per_day=2)
    async with TestClient(TestServer(m.make_app())) as client:
        app = client.server.app
        refs = []
        for i in range(4):
            _, b = await _call(client, "touch", key(f"robo-{i}"), caller_hint=f"+1555555017{i}",
                               fields={"caller_name": f"Robo {i}"})
            refs.append(b["ref"])
        calls, _ = m.ensure_agent(app)
        for r in calls.sweep(180, False, 120, now=agent_calls.now_ts() + 999):
            await m.deliver_final(app, r)
        emails = [k for k in _jobs(m, app) if k.startswith("email:")]
        assert len(emails) == 2
        for ref in refs:
            assert m.ensure_runtime(app).get("pa-" + ref.lower()) is not None


def test_hook_fallback_matches_caller_id_only(tmp_path):
    """I1: the spoken callback number never correlates a webhook."""
    calls = agent_calls.AgentCalls(tmp_path / "c.json")
    calls.touch(key("i1"), {"callback_number": "+15555550150"}, caller_hint="")
    assert calls.find_open_by_caller("+15555550150") == ""
    calls.touch(key("i1b"), {}, caller_hint="+15555550151")
    assert calls.find_open_by_caller("+15555550151") == key("i1b")


def test_fields_frozen_after_final(tmp_path):
    """I2: late values do not overwrite what was emailed; they become notes."""
    calls = agent_calls.AgentCalls(tmp_path / "c.json")
    k = key("i2")
    calls.agent_final(k, "message", {"caller_name": "First Name", "reason": "original"})
    res = calls.touch(k, {"caller_name": "Changed Name", "best_time": "after 5"})
    row = res["row"]
    assert res["late"] and res["changed"]
    assert row["fields"]["caller_name"] == "First Name" and row["fields"]["reason"] == "original"
    assert "Later caller name: Changed Name" in row["notes"]
    assert row["fields"]["best_time"] == "after 5"  # a new, empty field may still be filled in


@pytest.mark.asyncio
async def test_expired_jobs_push_one_masked_notice_and_show_count(mod):
    """R3-L1: expiries fold into ONE masked push that waits behind the gate; diag shows the count."""
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        helper = {"state": None}

        async def read_state(_app, _entity):
            return helper["state"]

        mod.read_entity_state = read_state
        sent = []

        async def fake(job):
            sent.append(job)
            return True, "sent", False

        mod._box(app)["senders"] = {"push": fake, "email": fake}
        _calls, outbox = mod.ensure_agent(app)
        refs = []
        for i in range(2):
            _, b = await _call(client, "final", key(f"exp{i}"), kind="message",
                               fields={"caller_name": f"Secret Name{i}", "reason": "x"}, caller_hint=CALLER)
            refs.append(b["ref"])

        def age_all():
            data = json.loads(outbox.path.read_text())
            for job in data["jobs"].values():
                if job["kind"] != "push_notice":
                    job["created_at"] -= mod.NOTIFY_HOLD_MAX_S + 5
            outbox._write(data)

        age_all()
        await mod.agent_tick(app)
        notices = [j for j in json.loads(outbox.path.read_text())["jobs"].values() if j["kind"] == "push_notice"]
        assert len(notices) == 1 and notices[0]["status"] == "pending" and notices[0]["count"] == 4
        # A later expiry while still held grows the same notice; the notice itself never expires.
        _, b = await _call(client, "final", key("exp-late"), kind="message",
                           fields={"caller_name": "Secret Late", "reason": "x"}, caller_hint=CALLER)
        refs.append(b["ref"])
        age_all()
        data = json.loads(outbox.path.read_text())
        for job in data["jobs"].values():
            job["created_at"] -= mod.NOTIFY_HOLD_MAX_S + 5
        outbox._write(data)
        await mod.agent_tick(app)
        notices = [j for j in json.loads(outbox.path.read_text())["jobs"].values() if j["kind"] == "push_notice"]
        assert len(notices) == 1 and notices[0]["status"] == "pending" and notices[0]["count"] == 6
        assert sent == []
        assert mod.agent_diag(app)["notices_expired"] == 6
        assert mod._box(app).get("sensor_dirty") is True
        # First send tick: exactly one push, masked, nothing from any call.
        helper["state"] = "off"
        await mod.agent_tick(app)
        await mod.agent_tick(app)
        assert len(sent) == 1 and sent[0]["kind"] == "push_notice"
        msg = sent[0]["payload"]["message"]
        assert msg == "6 CallCatch notices expired while the dry-run switch was unreadable.  Open CallCatch."
        blob = json.dumps(sent[0]["payload"])
        assert "Secret" not in blob and CALLER[-4:] not in blob
        for r in refs:
            assert r not in blob


@pytest.mark.asyncio
async def test_expiry_notice_is_dry_run_when_switch_on(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        _calls, outbox = mod.ensure_agent(app)
        outbox.note_expired(1)
        sent = []

        async def fake(job):
            sent.append(job)
            return True, "sent", False

        async def read_state(_app, _entity):
            return "on"

        mod.read_entity_state = read_state
        mod._box(app)["senders"] = {"push": fake}
        await mod.agent_tick(app)
        assert sent == [] and outbox.counts()["dry_run"] == 1
        assert outbox.note_expired(0) == ""


def test_expired_push_wording_singular():
    import notify_outbox

    assert notify_outbox.expired_push(1)["message"].startswith("1 CallCatch notice expired ")


@pytest.mark.asyncio
async def test_agentmail_send_carries_idempotency_key_per_ref():
    """R3-L2: Idempotency-Key callcatch-<ref>, same on every retry; 409 (in flight) retries."""
    import notify_render
    import notify_send

    row = {"ref": "CC-ACDEX", "created_at": 1_800_000_000, "fields": {"caller_name": "Idem", "reason": "x"}}
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    sess = _Sess([503, 409, 200])
    send = notify_send.agentmail_sender(lambda: sess, "callcatch-test@agentmail.to", "k" * 40, "callcatch-test@proton.me")
    job = {"id": "email:CC-ACDEX", "ref": "CC-ACDEX", "payload": mail}
    assert await send(job) == (False, "http-503", True)
    assert await send(job) == (False, "http-409", True)
    assert await send(job) == (True, "sent", False)
    keys = [h["Idempotency-Key"] for _u, _b, h in sess.calls]
    assert keys == ["callcatch-CC-ACDEX"] * 3
    bodies = [b for _u, b, _h in sess.calls]
    assert bodies[0] == bodies[1] == bodies[2]
    assert notify_send.idempotency_key({"id": "email:CC-ACDEX"}) == "callcatch-CC-ACDEX"
    for bad in ({"ref": "CC-acdex"}, {"ref": "../x"}, {"id": "push:CC-ACDEX"}, {}):
        assert notify_send.idempotency_key(bad) == ""


def test_percent_local_part_refused_in_code_and_schema():
    """I12."""
    import notify_send

    assert not notify_send.recipient_allowed("b%x@proton.me")
    assert notify_send.parse_recipient("b%x@proton.me") == ""
    cfg = (pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    line = next(x for x in cfg.splitlines() if x.strip().startswith("mail_to:"))
    assert "%" not in line
