"""CallCatch 0.6.0 follow-up inbox. Queue only. Fake 555 numbers only."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load

import call_summary as cs  # noqa: E402
import inbox_store  # noqa: E402

CALLER = "+15555550100"
SPOKEN = "555-555-0199"
SPOKEN_DIGITS = "5555550199"
USER = {"X-Remote-User-Id": "user-555"}
ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
GATE = (
    "This is Sam calling about the gate. Please call me back at 555-555-0199."
)
MEDICAL = (
    "This is Dana Reyes calling about a prescription refill. "
    "Please call me back at 555-555-0142."
)
NO_CALLBACK = "Hey it's Sam. No rush, just checking in about dinner Saturday."

QUEUE_KEYS = {
    "voicemail_id",
    "followup_state",
    "callback_status",
    "callback_requested_at",
    "from_masked",
    "display_name",
    "purpose",
    "urgency",
    "known_contact",
    "medical",
    "check_transcript",
    "alert_silent",
    "quiet_hold",
}


def _assert_no_full_number(blob: str) -> None:
    for number in (CALLER, "15555550100", "5555550100", SPOKEN, SPOKEN_DIGITS, "5555550142", "555-555-0142"):
        assert number not in blob, number


def _span_ok(base: str, span: dict | None) -> None:
    if not span:
        return
    assert base[span["start"]:span["end"]] == span["quote"]
    assert span["quote"]
    assert span["quote"] in base


@pytest.fixture
def mod(monkeypatch, tmp_path):
    loaded = _load(monkeypatch, tmp_path)
    loaded.SUPERVISOR_INGRESS_IP = "127.0.0.1"
    return loaded


def test_ingest_cannot_set_followup_state():
    item = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-inject",
            "from": CALLER,
            "followup_state": "in_progress",
            "history": [{"to": "approved", "number": SPOKEN_DIGITS, "purpose": "gate"}],
            "outcome_note": "sent",
            "transcript_text": GATE,
        }
    )
    assert item["followup_state"] == ""
    assert item["history"] == []
    assert item["outcome_note"] == ""
    assert inbox_store.default_followup(item) == "needs_callback"


def test_card_quotes_are_transcript_slices_and_blank_without_a_spoken_number():
    item = inbox_store.item_from_payload(
        {"voicemail_id": "vm-gate", "from": CALLER, "caller_title": "Sam Rivera", "transcript_text": GATE}
    )
    facts = cs.card(item)
    base = cs.strip_format(item["transcript"])
    assert facts["who"] == "Sam"
    assert facts["who_provenance"] == "said in message"
    assert facts["wants"] == "the gate"
    assert facts["callback"] == SPOKEN_DIGITS
    assert facts["medical"] is False
    for key in ("who_quote", "wants_quote", "callback_quote", "heard_span"):
        _span_ok(base, facts[key])
    assert facts["who"] == facts["who_quote"]["quote"]
    assert facts["wants"] == facts["wants_quote"]["quote"]

    silent = inbox_store.item_from_payload(
        {"voicemail_id": "vm-fyi", "from": CALLER, "transcript_text": NO_CALLBACK}
    )
    quiet = cs.card(silent)
    assert quiet["callback"] == ""
    assert quiet["callback_quote"] is None
    assert quiet["callback_display"] == ""
    # The older summary may still fall back to the caller id. The card does not.
    assert cs.extract_summary(NO_CALLBACK, CALLER)["callback"] == "15555550100"
    spoken_self = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-self",
            "from": CALLER,
            "transcript_text": "Call (555) 555-0100 if you want a different time.",
            "duration_sec": 20,
        }
    )
    assert inbox_store.default_followup(spoken_self) == "needs_callback"
    assert cs.card(spoken_self)["callback"] == "5555550100"


def test_medical_card_is_verbatim_and_draft_does_not_invent():
    item = inbox_store.item_from_payload(
        {"voicemail_id": "vm-med", "from": CALLER, "caller_title": "Dana Reyes", "transcript_text": MEDICAL}
    )
    facts = cs.card(item)
    base = cs.strip_format(item["transcript"])
    assert facts["medical"] is True
    assert facts["note"] == "Check transcript"
    assert facts["who"] == "Dana Reyes"
    assert facts["wants"] == "a prescription refill"
    assert facts["who"] in base and facts["wants"] in base
    assert facts["wants"] == facts["wants_quote"]["quote"]
    assert "diagnos" not in facts["wants"].lower()
    assert "tuesday" not in facts["wants"].lower()
    draft = cs.build_draft(facts)
    assert draft["queued_only"] is True
    assert "http" not in draft["text"].lower() and "http" not in draft["brief"].lower()
    assert "Check transcript" in draft["text"]
    assert "Check transcript" in draft["brief"]
    assert "tuesday" not in draft["text"].lower()
    assert "diagnos" not in draft["text"].lower()
    assert facts["wants"] in draft["text"]


def test_hangup_and_spam_are_not_actionable_and_quiet_hours_hold():
    hangup = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-hang",
            "from": CALLER,
            "transcript_text": "Please call me back.",
            "duration_sec": 4,
        }
    )
    assert cs.is_hangup(hangup) is True
    assert cs.alert_silent(hangup) is True
    assert inbox_store.default_followup(hangup) == "new"
    assert inbox_store.actionable_needs_callback(hangup) is False

    spam = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-spam",
            "from": CALLER,
            "transcript_text": GATE,
            "trust_verdict": "blocked",
            "duration_sec": 30,
        }
    )
    assert inbox_store.default_followup(spam) == "spam"
    assert cs.alert_silent(spam) is True
    assert inbox_store.actionable_needs_callback(spam) is False

    night = datetime(2026, 11, 6, 1, 15, tzinfo=ET)
    day = datetime(2026, 11, 6, 12, 0, tzinfo=ET)
    known = inbox_store.item_from_payload(
        {
            "voicemail_id": "vm-known",
            "from": CALLER,
            "caller_title": "Dana Reyes",
            "transcript_text": "This is Dana Reyes. The valve is leaking and it is urgent.",
            "duration_sec": 40,
        }
    )
    stranger = dict(known, caller_title="Unknown")
    assert cs.known_contact(known) is True
    assert cs.known_contact(stranger) is False
    assert cs.quiet_active(night) is True
    assert cs.quiet_active(datetime(2026, 11, 6, 6, 0, tzinfo=ET)) is False
    assert cs.quiet_hold(known, night) is False
    assert cs.quiet_hold(stranger, night) is True
    assert cs.quiet_hold(known, day) is False
    policy = cs.quiet_status(night)
    assert policy["tz"] == "America/New_York"
    assert policy["start"] == "00:00:00" and policy["end"] == "06:00:00"
    assert policy["exception"] == "urgent_known_contact"
    assert policy["active"] is True

    sensor = inbox_store.sensor_state([hangup, spam, known])
    assert sensor["attributes"]["needs_callback"] == 0
    row = sensor["attributes"]["voicemails"][0]
    assert "transcript" not in row and "history" not in row and "outcome_note" not in row
    _assert_no_full_number(repr(sensor))


def test_queue_row_metadata_is_masked_and_medical_purpose_is_empty():
    item = inbox_store.item_from_payload(
        {"voicemail_id": "vm-gate", "from": CALLER, "caller_title": "Sam Rivera", "transcript_text": GATE}
    )
    item["callback_status"] = "pending"
    item["followup_state"] = "queued"
    row = inbox_store.callback_queue_row(item, datetime(2026, 11, 6, 15, 0, tzinfo=ET))
    assert set(row) == QUEUE_KEYS
    assert row["from_masked"] == "•••0100"
    assert row["display_name"] == "Sam"
    assert row["purpose"] == "the gate"
    assert row["medical"] is False
    _assert_no_full_number(repr(row))

    medical = inbox_store.item_from_payload(
        {"voicemail_id": "vm-med", "from": CALLER, "caller_title": "Dana Reyes", "transcript_text": MEDICAL}
    )
    medical["callback_status"] = "pending"
    medical["followup_state"] = "queued"
    hidden = inbox_store.callback_queue_row(medical)
    assert hidden["purpose"] == ""
    assert hidden["check_transcript"] is True
    assert hidden["medical"] is True
    _assert_no_full_number(repr(hidden))


def test_transitions_reject_skips_and_cap_no_answer(tmp_path):
    store = inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")
    saved = store.upsert(
        inbox_store.item_from_payload(
            {"voicemail_id": "vm-gate", "from": CALLER, "caller_title": "Sam Rivera", "transcript_text": GATE}
        )
    )
    vid = saved["voicemail_id"]
    assert inbox_store.effective_followup(saved) == "needs_callback"

    fresh = store.upsert(
        inbox_store.item_from_payload(
            {"voicemail_id": "vm-new", "from": CALLER, "transcript_text": "Hello from the porch.", "duration_sec": 12}
        )
    )
    assert store.transition_followup(fresh["voicemail_id"], "in_progress")["error"] == "illegal"
    assert store.get(fresh["voicemail_id"])["followup_state"] in ("", "new")

    denied = store.transition_followup(vid, "in_progress")
    assert denied["error"] == "illegal"
    bad_number = store.transition_followup(vid, "approved", number="5555550101", purpose="the gate")
    assert bad_number["error"] == "illegal"

    queued = store.transition_followup(vid, "queued", actor="bryson")
    assert queued["changed"] is True and queued["fire_event"] is True
    assert queued["item"]["callback_status"] == "pending"
    assert queued["item"]["followup_state"] == "queued"
    again = store.transition_followup(vid, "queued")
    assert again["changed"] is False and again["fire_event"] is False

    assert store.transition_followup(vid, "draft_ready")["item"]["draft_id"].startswith("d")
    assert store.transition_followup(vid, "approved", number="5555550101", purpose="the gate")["error"] == "bad-number"
    assert store.transition_followup(vid, "approved", number=SPOKEN, purpose="https://example.invalid/x")["error"] == "bad-field"
    assert store.transition_followup(vid, "approved", number=SPOKEN, purpose="not in the message")["error"] == "bad-purpose"
    approved = store.transition_followup(vid, "approved", number=SPOKEN, purpose="the gate")
    assert approved["item"]["followup_state"] == "approved"
    trail = inbox_store.masked_history(approved["item"])
    assert trail[-1]["number_masked"] == "•••0199"
    assert trail[-1]["purpose"] == "the gate"
    assert SPOKEN_DIGITS not in json.dumps(trail)

    started = store.transition_followup(vid, "in_progress", call_ref="call-555")
    assert started["item"]["followup_state"] == "in_progress"
    assert started["item"]["call_ref"] == "call-555"
    missed = store.transition_followup(vid, "no_answer", outcome_note="no pickup")
    assert missed["item"]["followup_state"] == "no_answer"
    assert store.transition_followup(vid, "queued")["item"]["followup_state"] == "queued"
    store.transition_followup(vid, "draft_ready")
    store.transition_followup(vid, "approved", number=SPOKEN, purpose="the gate")
    store.transition_followup(vid, "in_progress")
    store.transition_followup(vid, "no_answer")
    assert store.transition_followup(vid, "queued")["item"]["followup_state"] == "queued"
    store.transition_followup(vid, "draft_ready")
    store.transition_followup(vid, "approved", number=SPOKEN, purpose="the gate")
    store.transition_followup(vid, "in_progress")
    store.transition_followup(vid, "no_answer")
    blocked = store.transition_followup(vid, "queued")
    assert blocked["error"] == "max-attempts"

    other = store.upsert(
        inbox_store.item_from_payload(
            {"voicemail_id": "vm-snooze", "from": CALLER, "caller_title": "Sam Rivera", "transcript_text": GATE}
        )
    )
    snoozed = store.transition_followup(
        other["voicemail_id"], "snoozed", snooze_until="2026-11-07T15:00:00+00:00"
    )
    assert snoozed["item"]["followup_state"] == "snoozed"
    assert snoozed["item"]["followup_prior"] == "needs_callback"
    assert inbox_store.in_callback_queue(snoozed["item"]) is False
    woke = store.transition_followup(other["voicemail_id"], "needs_callback")
    assert woke["item"]["followup_state"] == "needs_callback"
    assert store.mark_heard(fresh["voicemail_id"]) is True
    assert store.get(fresh["voicemail_id"])["followup_state"] == "seen"


def test_request_callback_still_queues_and_reingest_keeps_it(tmp_path):
    store = inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")
    saved = store.upsert(
        inbox_store.item_from_payload(
            {"voicemail_id": "vm-gate", "from": CALLER, "caller_title": "Sam Rivera", "transcript_text": GATE}
        )
    )
    first = store.request_callback(saved["voicemail_id"])
    assert first["created"] is True
    assert first["item"]["callback_status"] == "pending"
    assert first["item"]["followup_state"] == "queued"
    second = store.request_callback(saved["voicemail_id"])
    assert second["created"] is False
    assert second["item"]["callback_requested_at"] == first["item"]["callback_requested_at"]
    refreshed = store.upsert(
        inbox_store.item_from_payload(
            {
                "voicemail_id": saved["voicemail_id"],
                "from": CALLER,
                "transcript_text": "follow up",
                "followup_state": "done",
                "callback_status": "returned",
            }
        )
    )
    assert refreshed["callback_status"] == "pending"
    assert refreshed["followup_state"] == "queued"


def test_ui_draft_does_not_fetch_and_filters_exist():
    js = (ROOT / "rootfs" / "app" / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "rootfs" / "app" / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "rootfs" / "app" / "static" / "style.css").read_text(encoding="utf-8")
    start = js.index("function showDraft")
    end = js.index("async function setFollowup")
    draft_fn = js[start:end]
    assert "fetch" not in draft_fn and "api(" not in draft_fn
    assert "Queue only" in html
    for label in ("Needs callback", "Urgent", "Spam", "Done"):
        assert label in html
    assert "followup-badge" in html
    assert "@media (max-width: 400px)" in css
    assert "@media (min-width: 900px)" in css
    assert "--bg: #000" in css or "--bg:#000" in css


@pytest.mark.asyncio
async def test_followup_http_and_callback_list_are_admin_reads(mod):
    fired = []

    async def fake_fire(_session, event_type, data):
        fired.append((event_type, data))
        return 200, {"ok": True, "mock": True}

    async def fake_service(*_args, **_kwargs):
        raise AssertionError("follow-up must not call a service")

    mod.ha_fire_event = fake_fire
    mod.ha_call_service = fake_service
    async with TestClient(TestServer(mod.make_app())) as client:
        store = mod.ensure_runtime(client.server.app)
        saved = store.upsert(
            inbox_store.item_from_payload(
                {
                    "voicemail_id": "vm-gate",
                    "from": CALLER,
                    "caller_title": "Sam Rivera",
                    "transcript_text": GATE,
                    "duration_sec": 30,
                }
            )
        )
        store.upsert(
            inbox_store.item_from_payload(
                {"voicemail_id": "vm-fyi", "from": CALLER, "transcript_text": NO_CALLBACK, "duration_sec": 20}
            )
        )
        vid = saved["voicemail_id"]
        assert (await client.post("/api/followup", json={"id": vid, "state": "queued"})).status == 403
        assert (await client.get("/api/callback_requests")).status == 403
        assert (await client.get("/api/callback_status", headers=USER)).status == 404
        assert (await client.post("/api/callback_requests", json={}, headers=USER)).status == 405

        early = await client.post("/api/followup", json={"id": vid, "state": "in_progress"}, headers=USER)
        assert early.status == 409
        assert (await early.json())["error"] == "illegal"

        queued = await client.post(
            "/api/followup",
            json={"id": vid, "state": "queued", "actor": "agent"},
            headers=USER,
        )
        body = await queued.json()
        assert queued.status == 200
        assert body["followup_state"] == "queued"
        assert body["callback_status"] == "pending"
        assert body["event_fired"] is True
        assert fired and fired[0][0] == "callcatch_callback_requested"
        assert set(fired[0][1]) == {"voicemail_id", "from_masked", "display_name", "summary", "requested_at"}
        _assert_no_full_number(repr(fired[0][1]))

        listed = await client.get("/api/callback_requests", headers=USER)
        payload = await listed.json()
        assert listed.status == 200
        assert payload["ok"] is True and payload["v"] == "0.6.0"
        assert payload["count"] == 1
        assert [row["voicemail_id"] for row in payload["callbacks"]] == [vid]
        assert set(payload["callbacks"][0]) == QUEUE_KEYS
        assert payload["quiet"]["tz"] == "America/New_York"
        assert payload["quiet"]["start"] == "00:00:00"
        assert payload["quiet"]["end"] == "06:00:00"
        assert payload["quiet"]["exception"] == "urgent_known_contact"
        assert "transcript" not in payload["callbacks"][0]
        _assert_no_full_number(json.dumps(payload))

        unsafe = await client.post(
            "/api/followup",
            json={"id": vid, "state": "draft_ready", "purpose": "javascript:alert(1)"},
            headers=USER,
        )
        assert unsafe.status == 400
        assert (await unsafe.json())["error"] == "bad-field"
