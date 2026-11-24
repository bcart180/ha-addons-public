"""0.8.6 GDIT work-hours copy: gates, redaction, pins, and after-Proton send.

Fake data only: 555-01xx numbers and invented names.
"""
from __future__ import annotations

import logging
import pathlib
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import gdit_copy  # noqa: E402
import notify_outbox  # noqa: E402
import notify_render  # noqa: E402
import notify_send  # noqa: E402

AGENT = "agent-token-555-" + "x" * 24
TS = 1_795_558_500  # 2026-11-24 17:15:00 America/New_York
REF = "CC-G0T01"
CALLER = "+15555550142"


def _row(**extra) -> dict:
    row = {
        "ref": REF,
        "created_at": TS,
        "duration_s": 42,
        "hung_up_early": False,
        "has_audio": True,
        "caller_name_source": "contact",
        "summary": "Jordan asked if Saturday afternoon still works for pickup.",
        "transcript": "Caller: Hi, this is Jordan Hale.  Can I come by after 3?",
        "fields": {
            "caller_name": "Jordan Hale",
            "caller_id": CALLER,
            "callback_number": CALLER,
        },
        "notes": [],
    }
    fields = dict(row["fields"])
    if "fields" in extra:
        fields.update(extra.pop("fields"))
    row.update(extra)
    row["fields"] = fields
    return row


# --- work and proton gates ------------------------------------------------

def test_work_gate_only_explicit_on():
    assert gdit_copy.work_gate(False, "on") == "disabled"
    assert gdit_copy.work_gate(True, "on") == "ok"
    assert gdit_copy.work_gate(True, "off") == "not_at_work"
    for state in (None, "unknown", "unavailable", "", "ON", "true", "home"):
        assert gdit_copy.work_gate(True, state) == "work_unknown", state


def test_decide_requires_proton_then_work_then_sensitivity():
    row = _row()
    assert gdit_copy.decide(True, "pending", "on", row) == "no_proton"
    assert gdit_copy.decide(True, "", "on", row) == "no_proton"
    assert gdit_copy.decide(False, "sent", "on", row) == "disabled"
    assert gdit_copy.decide(True, "sent", None, row) == "work_unknown"
    assert gdit_copy.decide(True, "would-send", "unavailable", row) == "work_unknown"
    assert gdit_copy.decide(True, "sent", "off", row) == "not_at_work"
    assert gdit_copy.decide(True, "would-send", "on", row) == "dry_run"
    assert gdit_copy.decide(True, "sent", "on", row) == "ok"


# --- sensitive skip -------------------------------------------------------

@pytest.mark.parametrize(
    "field,value",
    [
        ("fields", {"caller_name": "VA Hospital Scheduling"}),
        ("fields", {"caller_label": "Community Clinic"}),
        ("fields", {"label": "Wells Fargo Card Services"}),
        ("fields", {"org": "Internal Revenue Service"}),
        ("fields", {"labels": ["Primary", "Pharmacy"]}),
        ("summary", "They called from the VA clinic about an appointment."),
        ("transcript", "Caller: This is the Veterans Affairs benefits line."),
        ("transcript", "Caller: I am with Quest Diagnostics about lab results."),
        ("summary", "A mortgage specialist asked to talk today."),
        ("transcript", "Caller: Calling from Capital One about a credit card."),
    ],
)
def test_sensitive_matches_name_label_summary_transcript(field, value):
    if field == "fields":
        row = _row(fields=value)
    else:
        row = _row(**{field: value})
    assert gdit_copy.is_sensitive(row) is True
    assert gdit_copy.decide(True, "sent", "on", row) == "sensitive"


def test_notes_are_not_scanned_for_sensitivity():
    row = _row(notes=["Called from the hospital about a prescription."])
    assert gdit_copy.is_sensitive(row) is False
    assert gdit_copy.decide(True, "sent", "on", row) == "ok"


@pytest.mark.parametrize(
    "text",
    [
        "Vanessa is dropping by later.",
        "The bank of the river is muddy.",
        "I saw my doctor yesterday, nothing urgent.",
        "Calling about a food bank donation.",
        "This is Ira from next door.",
        "Dr. Smith is my neighbor.",
    ],
)
def test_conservative_keywords_do_not_fire_on_everyday_words(text):
    assert gdit_copy.is_sensitive(_row(summary=text, transcript=text)) is False


# --- redaction ------------------------------------------------------------

def test_redacted_template_is_name_time_and_proton_pointer():
    mail = notify_render.gdit_email(_row())
    assert mail["subject"] == "Jordan Hale called at 5:15 PM ET"
    assert mail["text"] == "Jordan Hale called at 5:15 PM ET.\n\nSee Proton for the full message."
    assert "See Proton for the full message." in mail["html"]
    assert mail["to"] == ["bryson.carter@gdit.com"]
    assert mail["category"] == "callcatch_gdit_notice"
    blob = mail["subject"] + mail["text"] + mail["html"]
    assert "555" not in blob
    assert "0142" not in blob
    assert "last-4" not in blob.lower()
    assert "Number ending" not in blob
    assert "Saturday" not in blob
    assert "transcript" not in blob.lower()
    assert "Assistant:" not in blob and "Caller:" not in blob
    assert "recording" not in blob.lower()
    assert "ha.home.arpa" not in blob
    assert "hassio/ingress" not in blob
    assert "http" not in blob.lower()
    assert "+1" not in blob


@pytest.mark.parametrize("source", ["cnam", "spoken", "", "unknown"])
def test_cnam_and_spoken_become_a_caller(source):
    row = _row(caller_name_source=source)
    assert notify_render.gdit_display_name(row) == "A caller"
    mail = notify_render.gdit_email(row)
    assert mail["subject"].startswith("A caller called at ")
    assert "Jordan Hale" not in mail["subject"]
    assert "Jordan Hale" not in mail["text"]
    assert "Jordan Hale" not in mail["html"]
    assert "(caller ID)" not in mail["subject"]


def test_unusable_contact_name_becomes_a_caller():
    row = _row(fields={"caller_name": "•••0142"}, caller_name_source="contact")
    assert notify_render.gdit_display_name(row) == "A caller"


# --- recipient pins -------------------------------------------------------

def test_proton_pin_unchanged_and_refuses_gdit():
    assert notify_send.RECIPIENT_DOMAINS == ("proton.me", "protonmail.com")
    assert "gdit.com" not in notify_send.RECIPIENT_DOMAINS
    assert notify_send.parse_recipient("bryson.carter@gdit.com") == ""
    assert notify_send.recipient_allowed("bryson.carter@gdit.com") is False
    assert notify_send.parse_recipient("callcatch-test@proton.me") == "callcatch-test@proton.me"
    body = notify_send.build_agentmail_body(
        {
            "to": ["bryson.carter@gdit.com"],
            "subject": "x",
            "text": "x",
            "html": "<p>x</p>",
            "category": notify_send.CATEGORY,
        },
        "bryson.carter@gdit.com",
    )
    assert body is None


def test_gdit_pin_is_exact_address_and_domain():
    assert notify_send.GDIT_DOMAINS == ("gdit.com",)
    assert notify_send.parse_gdit_recipient("bryson.carter@gdit.com") == "bryson.carter@gdit.com"
    assert notify_send.parse_gdit_recipient("Bryson.Carter@GDIT.com") == "bryson.carter@gdit.com"
    for bad in (
        "other@gdit.com",
        "bryson.carter@mail.gdit.com",
        "bryson.carter@gdit.com.example",
        "bryson.carter@proton.me",
        "bryson.carter@gdit.com,other@gdit.com",
        "someone@gmail.com",
        "",
    ):
        assert notify_send.parse_gdit_recipient(bad) == "", bad
        assert notify_send.gdit_recipient_allowed(bad) is False


def test_mail_to_schema_stays_proton_only():
    config = (pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    assert "mail_to: match(^[A-Za-z0-9._+\\-]{1,64}@(proton\\.me|protonmail\\.com)$)?" in config
    assert "gdit_work_copy: false" in config
    assert "gdit_work_copy: bool" in config


def test_gdit_body_and_idempotency_are_distinct():
    payload = notify_render.gdit_email(_row())
    body = notify_send.build_gdit_agentmail_body(payload, notify_send.GDIT_RECIPIENT)
    assert body is not None
    assert body["to"] == ["bryson.carter@gdit.com"]
    assert body["labels"] == ["callcatch", "callcatch_gdit_notice"]
    protonish = dict(payload)
    protonish["category"] = notify_send.CATEGORY
    assert notify_send.build_gdit_agentmail_body(protonish, notify_send.GDIT_RECIPIENT) is None
    job = {"id": f"gdit:{REF}", "ref": REF}
    assert notify_send.idempotency_key({"id": f"email:{REF}", "ref": REF}) == f"callcatch-{REF}"
    assert notify_send.gdit_idempotency_key(job) == f"callcatch-gdit-{REF}"
    assert notify_send.idempotency_key(job) != notify_send.gdit_idempotency_key(job)


# --- after Proton, dry-run, and logging ----------------------------------

def _mod(monkeypatch, tmp_path, **extra):
    return _load(
        monkeypatch,
        tmp_path,
        agent_token=AGENT,
        push_service="mobile_app_test_phone",
        mail_backend="agentmail",
        mail_to="callcatch-test@proton.me",
        agentmail_inbox="callcatch-test@agentmail.to",
        agentmail_api_key="am-key-555-not-real",
        gdit_work_copy=True,
        **extra,
    )


async def _finalize(mod, client, label="gdit"):
    import hashlib

    key = hashlib.sha256(label.encode()).hexdigest()[:32]
    resp = await client.post(
        "/api/agent/call",
        json={
            "op": "final",
            "call_key": key,
            "kind": "message",
            "caller_hint": CALLER,
            "fields": {"caller_name": "Jordan Hale", "reason": "pickup"},
            "transcript": "Caller: Hi, this is Jordan Hale.",
        },
        headers={"Authorization": f"Bearer {AGENT}"},
    )
    body = await resp.json()
    assert resp.status == 200, body
    return body["ref"]


def _state_fn(dry_run: str, work: str | None):
    async def read_state(_app, entity):
        if entity == "input_boolean.callcatch_notify_dry_run":
            return dry_run
        if entity == gdit_copy.WORK_SENSOR:
            return work
        return None

    return read_state


@pytest.mark.asyncio
async def test_option_default_off_is_quiet_disabled(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _load(
        monkeypatch,
        tmp_path,
        agent_token=AGENT,
        push_service="mobile_app_test_phone",
        mail_to="callcatch-test@proton.me",
        agentmail_inbox="callcatch-test@agentmail.to",
    )
    assert mod.GDIT_WORK_COPY is False
    assert mod.VERSION == "0.8.6"
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        mod.read_entity_state = _state_fn("off", "on")
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        await _finalize(mod, client, "default-off")
        await mod.agent_tick(app)
        assert any(i.startswith("email:") for i in sent)
        assert not any(i.startswith("gdit:") for i in sent)
        assert "gdit" not in caplog.text


@pytest.mark.asyncio
async def test_dry_run_logs_would_send_only(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        mod.read_entity_state = _state_fn("on", "on")
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        ref = await _finalize(mod, client, "dry")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert sent == []
        assert outbox.get(f"email:{ref}")["status"] == "dry_run"
        assert outbox.get(f"gdit:{ref}") is None
        assert "gdit would-send reason=dry_run" in caplog.text
        assert "Jordan" not in caplog.text
        assert "555" not in caplog.text
        assert "gdit.com" not in caplog.text


@pytest.mark.asyncio
async def test_sent_after_proton_enqueues_and_sends_gdit(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            if job["kind"] == "gdit":
                payload = job["payload"]
                assert payload["to"] == ["bryson.carter@gdit.com"]
                assert "See Proton for the full message." in payload["text"]
                assert "555" not in payload["text"]
                assert "Saturday" not in payload.get("html", "")
            return True, "sent", False

        mod.read_entity_state = _state_fn("off", "on")
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        ref = await _finalize(mod, client, "send")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert f"email:{ref}" in sent
        assert f"gdit:{ref}" in sent
        # Proton first.
        assert sent.index(f"email:{ref}") < sent.index(f"gdit:{ref}")
        assert outbox.get(f"gdit:{ref}")["status"] == "sent"
        assert "gdit sent reason=sent" in caplog.text
        assert "Jordan" not in caplog.text
        assert "555" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work,reason",
    [
        ("off", "not_at_work"),
        ("unknown", "work_unknown"),
        ("unavailable", "work_unknown"),
        (None, "work_unknown"),
        ("", "work_unknown"),
    ],
)
async def test_work_sensor_fail_closed_skips_copy(monkeypatch, tmp_path, caplog, work, reason):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        mod.read_entity_state = _state_fn("off", work)
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        ref = await _finalize(mod, client, f"work-{work}")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert f"email:{ref}" in sent
        assert f"gdit:{ref}" not in sent
        assert outbox.get(f"gdit:{ref}") is None
        assert f"gdit skip reason={reason}" in caplog.text
        assert "Jordan" not in caplog.text


@pytest.mark.asyncio
async def test_sensitive_caller_is_skipped_after_proton(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        import hashlib

        mod.read_entity_state = _state_fn("off", "on")
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        key = hashlib.sha256(b"clinic").hexdigest()[:32]
        resp = await client.post(
            "/api/agent/call",
            json={
                "op": "final",
                "call_key": key,
                "kind": "message",
                "caller_hint": CALLER,
                "fields": {"caller_name": "Riverside Clinic"},
                "transcript": "Caller: Confirming a dental appointment.",
            },
            headers={"Authorization": f"Bearer {AGENT}"},
        )
        body = await resp.json()
        ref = body["ref"]
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert f"email:{ref}" in sent
        assert f"gdit:{ref}" not in sent
        assert outbox.get(f"gdit:{ref}") is None
        assert "gdit skip reason=sensitive" in caplog.text
        assert "Riverside" not in caplog.text
        assert "dental" not in caplog.text.lower()


@pytest.mark.asyncio
async def test_no_proton_means_no_gdit(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    # Email job stays pending because the email sender is missing.
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        mod.read_entity_state = _state_fn("off", "on")
        mod._box(app)["senders"] = {"push": fake, "gdit": fake}
        ref = await _finalize(mod, client, "no-proton")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert outbox.get(f"email:{ref}")["status"] == "pending"
        assert outbox.get(f"gdit:{ref}") is None
        assert not any(i.startswith("gdit:") for i in sent)
        assert "gdit would-send" not in caplog.text
        assert "gdit enqueue" not in caplog.text
        assert "gdit sent" not in caplog.text


def test_outbox_accepts_gdit_kind():
    assert "gdit" in notify_outbox.KINDS
    assert notify_outbox.ID_RE.fullmatch("gdit:CC-AB12C")
    assert not notify_outbox.ID_RE.fullmatch("email:CC-EXAMPLE")
