"""0.8.8 GDIT presence notice: zone gate, content-free body, pins, after-Proton send.

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
IN_ZONE = [gdit_copy.WORK_PERSON]


def _row(**extra) -> dict:
    row = {
        "ref": REF,
        "created_at": TS,
        "duration_s": 42,
        "hung_up_early": False,
        "has_audio": True,
        "summary": "Jordan asked if Saturday afternoon still works for pickup.",
        "transcript": "Caller: Hi, this is Jordan Hale.  Can I come by after 3?",
        "fields": {
            "caller_name": "Jordan Hale",
            "caller_name_source": "contact",
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


def test_work_gate_only_work_zone():
    assert gdit_copy.work_gate(False, "Work", IN_ZONE) == "disabled"
    assert gdit_copy.work_gate(True, "Work", IN_ZONE) == "ok"
    assert gdit_copy.work_gate(True, "work", IN_ZONE) == "ok"
    assert gdit_copy.work_gate(True, "WORK", IN_ZONE) == "ok"
    assert gdit_copy.work_gate(True, "home", IN_ZONE) == "not_at_work"
    assert gdit_copy.work_gate(True, "not_home", []) == "not_at_work"
    assert gdit_copy.work_gate(True, "on", []) == "not_at_work"
    for state in (None, "unknown", "unavailable", "", "none"):
        assert gdit_copy.work_gate(True, state, IN_ZONE) == "work_unknown", state


def test_work_gate_requires_zone_work_persons():
    assert gdit_copy.WORK_ZONE_ID == "zone.work"
    # A second zone also named Work: person state matches, but zone.work is empty.
    assert gdit_copy.work_gate(True, "Work", []) == "not_at_work"
    assert gdit_copy.work_gate(True, "Work", ["person.other"]) == "not_at_work"
    assert gdit_copy.work_gate(True, "Work", None) == "work_unknown"
    assert gdit_copy.work_gate(True, "Work", "person.bryson") == "work_unknown"
    assert gdit_copy.work_gate(True, "Work", {"persons": IN_ZONE}) == "work_unknown"
    # Entity id match is case-insensitive.
    assert gdit_copy.work_gate(True, "Work", ["Person.Bryson"]) == "ok"


def test_decide_requires_proton_then_presence():
    assert gdit_copy.decide(True, "pending", "Work", IN_ZONE) == "no_proton"
    assert gdit_copy.decide(True, "", "Work", IN_ZONE) == "no_proton"
    assert gdit_copy.decide(False, "sent", "Work", IN_ZONE) == "disabled"
    assert gdit_copy.decide(True, "sent", None, IN_ZONE) == "work_unknown"
    assert gdit_copy.decide(True, "would-send", "unavailable", IN_ZONE) == "work_unknown"
    assert gdit_copy.decide(True, "sent", "home", []) == "not_at_work"
    assert gdit_copy.decide(True, "sent", "Work", None) == "work_unknown"
    assert gdit_copy.decide(True, "sent", "Work", []) == "not_at_work"
    assert gdit_copy.decide(True, "would-send", "Work", IN_ZONE) == "dry_run"
    assert gdit_copy.decide(True, "sent", "Work", IN_ZONE) == "ok"


def test_voicemail_left_uses_has_audio_rule_for_agent_rows():
    agent = {
        "ref": REF,
        "created_at": TS,
        "recording_file": "CC-G0T01.wav",
        "verbatim_at": TS,
    }
    assert "has_audio" not in agent
    assert notify_render._has_audio(agent) is True
    assert gdit_copy.voicemail_left(agent) is True
    assert notify_render.voicemail_left_label(agent) == "Yes"
    mail = notify_render.gdit_email(agent)
    assert "Voicemail left: Yes" in mail["text"]
    assert "Voicemail left: Yes" in mail["html"]
    assert notify_send.gdit_notice_is_clean(mail["subject"], mail["text"], mail["html"]) is True

    quiet = {"ref": REF, "created_at": TS, "recording_file": "", "verbatim_at": 0}
    assert notify_render._has_audio(quiet) is False
    assert gdit_copy.voicemail_left(quiet) is False
    assert "Voicemail left: No" in notify_render.gdit_email(quiet)["text"]

    assert gdit_copy.voicemail_left({"ref": REF, "verbatim_at": TS}) is True
    assert gdit_copy.voicemail_left({"ref": REF, "recording_file": "CC-G0T01.wav"}) is True
    assert gdit_copy.voicemail_left({"ref": REF, "has_audio": True}) is True
    assert gdit_copy.voicemail_left({"ref": REF, "has_audio": False}) is False
    assert gdit_copy.voicemail_left({}) is False
    assert gdit_copy.voicemail_left(None) is False


def test_content_free_notice_has_no_name_number_transcript_or_link():
    mail = notify_render.gdit_email(_row())
    assert mail["subject"] == "Missed call at 17:15 ET"
    assert mail["text"] == "Missed call\n\nVoicemail left: Yes\n\nTime: Tue Nov 24, 17:15 ET"
    assert "Voicemail left: Yes" in mail["html"]
    assert "Time: Tue Nov 24, 17:15 ET" in mail["html"]
    blob = mail["subject"] + mail["text"] + mail["html"]
    assert "Jordan" not in blob
    assert "Hale" not in blob
    assert "555" not in blob
    assert "0142" not in blob
    assert "Saturday" not in blob
    assert "transcript" not in blob.lower()
    assert "http" not in blob.lower()
    assert "href=" not in blob.lower()
    assert "proton" not in blob.lower()
    assert "@" not in mail["text"] and "@" not in mail["html"]
    assert notify_send.gdit_notice_is_clean(mail["subject"], mail["text"], mail["html"]) is True
    empty = notify_render.gdit_email(_row(has_audio=False, transcript="", summary=""))
    assert "Voicemail left: No" in empty["text"]
    assert empty["subject"] == "Missed call at 17:15 ET"


def test_legacy_redacted_payload_is_refused():
    payload = notify_render.gdit_email(_row())
    payload = dict(payload)
    payload["subject"] = "Jordan Hale called at 17:15 ET"
    payload["text"] = "Jordan Hale called at 17:15 ET.\n\nSee Proton for the full message."
    payload["html"] = "<p>See Proton for the full message.</p>"
    assert notify_send.build_gdit_agentmail_body(payload, notify_send.GDIT_RECIPIENT) is None


def test_proton_pin_unchanged_and_refuses_gdit():
    assert notify_send.parse_recipient("callcatch-test@proton.me") == "callcatch-test@proton.me"
    assert notify_send.parse_recipient("callcatch-test@protonmail.com") == "callcatch-test@protonmail.com"
    assert notify_send.parse_recipient(gdit_copy.GDIT_RECIPIENT) == ""
    assert notify_send.sender_allowed("callcatch-notices@agentmail.to") is True


def test_gdit_pin_is_exact_address_and_domain():
    pin = gdit_copy.GDIT_RECIPIENT
    local, _, domain = pin.partition("@")
    assert not hasattr(notify_send, "GDIT_DOMAINS")
    assert notify_send.GDIT_RECIPIENT is pin
    assert notify_send.parse_gdit_recipient(pin) == pin
    assert notify_send.parse_gdit_recipient(pin.upper()) == pin
    assert notify_send.parse_gdit_recipient(pin.swapcase()) == pin
    for bad in (
        f"other@{domain}",
        f"{local}@mail.{domain}",
        pin + ".example",
        f"{local}@proton.me",
        pin + f",other@{domain}",
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
    assert body["to"] == [gdit_copy.GDIT_RECIPIENT]
    assert body["labels"] == ["callcatch", "callcatch_gdit_notice"]
    protonish = dict(payload)
    protonish["category"] = notify_send.CATEGORY
    assert notify_send.build_gdit_agentmail_body(protonish, notify_send.GDIT_RECIPIENT) is None
    job = {"id": f"gdit:{REF}", "ref": REF}
    assert notify_send.idempotency_key({"id": f"email:{REF}", "ref": REF}) == f"callcatch-{REF}"
    assert notify_send.gdit_idempotency_key(job) == f"callcatch-gdit-{REF}"
    assert notify_send.idempotency_key(job) != notify_send.gdit_idempotency_key(job)


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


def _patch_ha(mod, dry_run: str, presence: str | None, zone_persons=...):
    if zone_persons is ...:
        if presence is not None and str(presence).strip().casefold() in gdit_copy.WORK_ZONE_STATES:
            zone_persons = list(IN_ZONE)
        elif presence in (None, "", "unknown", "unavailable", "none"):
            zone_persons = None
        else:
            zone_persons = []

    async def read_state(_app, entity):
        if entity == "input_boolean.callcatch_notify_dry_run":
            return dry_run
        if entity == gdit_copy.WORK_PERSON:
            return presence
        return None

    async def read_persons(_app, entity_id):
        if entity_id != gdit_copy.WORK_ZONE_ID:
            return None
        return zone_persons

    mod.read_entity_state = read_state
    mod.read_zone_persons = read_persons


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
    assert mod.VERSION == "0.8.8"
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        _patch_ha(mod, "off", "Work")
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

        _patch_ha(mod, "on", "Work")
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
                assert payload["to"] == [gdit_copy.GDIT_RECIPIENT]
                assert payload["subject"] == "Missed call at 17:15 ET" or payload["subject"].startswith("Missed call at ")
                assert "Voicemail left:" in payload["text"]
                assert "Jordan" not in payload["text"]
                assert "555" not in payload["text"]
                assert "http" not in payload.get("html", "").lower()
            return True, "sent", False

        _patch_ha(mod, "off", "Work")
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        ref = await _finalize(mod, client, "send")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert f"email:{ref}" in sent
        assert f"gdit:{ref}" in sent
        assert sent.index(f"email:{ref}") < sent.index(f"gdit:{ref}")
        assert outbox.get(f"gdit:{ref}")["status"] == "sent"
        assert "gdit sent reason=sent" in caplog.text
        assert "Jordan" not in caplog.text
        assert "555" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "presence,reason",
    [
        ("home", "not_at_work"),
        ("not_home", "not_at_work"),
        ("unknown", "work_unknown"),
        ("unavailable", "work_unknown"),
        (None, "work_unknown"),
        ("", "work_unknown"),
    ],
)
async def test_presence_fail_closed_skips_notice(monkeypatch, tmp_path, caplog, presence, reason):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        _patch_ha(mod, "off", presence)
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        ref = await _finalize(mod, client, f"work-{presence}")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert f"email:{ref}" in sent
        assert f"gdit:{ref}" not in sent
        assert outbox.get(f"gdit:{ref}") is None
        assert f"gdit skip reason={reason}" in caplog.text
        assert "Jordan" not in caplog.text


@pytest.mark.asyncio
async def test_no_proton_means_no_gdit(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        _patch_ha(mod, "off", "Work")
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


def test_stale_gdit_job_is_refused_and_not_retried():
    now = 1_800_000_000
    fresh = {"created_at": now - 60}
    stale = {"created_at": now - (15 * 60 + 1)}
    missing = {}
    assert gdit_copy.job_is_stale(fresh, now=now) is False
    assert gdit_copy.job_is_stale(stale, now=now) is True
    assert gdit_copy.job_is_stale(missing, now=now) is True


@pytest.mark.asyncio
async def test_stale_gdit_sender_does_not_send():
    posted = []

    class _Dead:
        def post(self, *args, **kwargs):
            posted.append(True)
            raise AssertionError("stale job must not be posted")

    async def send_job():
        async def presence():
            return "Work", IN_ZONE

        sender = notify_send.agentmail_gdit_sender(
            lambda: _Dead(), "callcatch-test@agentmail.to", "am-key-555-not-real",
            presence_check=presence,
        )
        job = {
            "id": f"gdit:{REF}",
            "ref": REF,
            "created_at": 1.0,
            "payload": notify_render.gdit_email(_row()),
        }
        return await sender(job)

    ok, code, retry = await send_job()
    assert (ok, code, retry) == (False, "stale", False)
    assert posted == []


@pytest.mark.asyncio
async def test_send_time_presence_recheck_fails_closed():
    posted = []

    class _Dead:
        def post(self, *args, **kwargs):
            posted.append(True)
            raise AssertionError("must not post when no longer at work")

    async def presence():
        return "home", []

    sender = notify_send.agentmail_gdit_sender(
        lambda: _Dead(), "callcatch-test@agentmail.to", "am-key-555-not-real",
        presence_check=presence,
    )
    import time
    job = {
        "id": f"gdit:{REF}",
        "ref": REF,
        "created_at": time.time(),
        "payload": notify_render.gdit_email(_row()),
    }
    ok, code, retry = await sender(job)
    assert (ok, code, retry) == (False, "not_at_work", False)
    assert posted == []


@pytest.mark.asyncio
async def test_send_time_requires_zone_work_membership():
    posted = []

    class _Dead:
        def post(self, *args, **kwargs):
            posted.append(True)
            raise AssertionError("must not post when zone.work persons is empty")

    async def presence():
        return "Work", []

    sender = notify_send.agentmail_gdit_sender(
        lambda: _Dead(), "callcatch-test@agentmail.to", "am-key-555-not-real",
        presence_check=presence,
    )
    import time
    job = {
        "id": f"gdit:{REF}",
        "ref": REF,
        "created_at": time.time(),
        "payload": notify_render.gdit_email(_row()),
    }
    ok, code, retry = await sender(job)
    assert (ok, code, retry) == (False, "not_at_work", False)
    assert posted == []


@pytest.mark.asyncio
async def test_send_time_zone_read_failure_is_work_unknown():
    posted = []

    class _Dead:
        def post(self, *args, **kwargs):
            posted.append(True)
            raise AssertionError("must not post when zone.work cannot be read")

    async def presence():
        return "Work", None

    sender = notify_send.agentmail_gdit_sender(
        lambda: _Dead(), "callcatch-test@agentmail.to", "am-key-555-not-real",
        presence_check=presence,
    )
    import time
    job = {
        "id": f"gdit:{REF}",
        "ref": REF,
        "created_at": time.time(),
        "payload": notify_render.gdit_email(_row()),
    }
    ok, code, retry = await sender(job)
    assert (ok, code, retry) == (False, "work_unknown", False)
    assert posted == []


def test_presence_check_is_required():
    import inspect

    param = inspect.signature(notify_send.agentmail_gdit_sender).parameters["presence_check"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


@pytest.mark.asyncio
async def test_missing_presence_check_fails_closed():
    posted = []

    class _Dead:
        def post(self, *args, **kwargs):
            posted.append(True)
            raise AssertionError("must not post without a presence check")

    sender = notify_send.agentmail_gdit_sender(
        lambda: _Dead(), "callcatch-test@agentmail.to", "am-key-555-not-real",
        presence_check=None,
    )
    import time
    job = {
        "id": f"gdit:{REF}",
        "ref": REF,
        "created_at": time.time(),
        "payload": notify_render.gdit_email(_row()),
    }
    ok, code, retry = await sender(job)
    assert (ok, code, retry) == (False, "work_unknown", False)
    assert posted == []


@pytest.mark.asyncio
async def test_presence_fail_closed_when_work_state_but_not_in_zone_work(
        monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    mod = _mod(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        sent = []

        async def fake(job):
            sent.append(job["id"])
            return True, "sent", False

        _patch_ha(mod, "off", "Work", zone_persons=[])
        mod._box(app)["senders"] = {"push": fake, "email": fake, "gdit": fake}
        ref = await _finalize(mod, client, "other-work-zone")
        await mod.agent_tick(app)
        _calls, outbox = mod.ensure_agent(app)
        assert f"email:{ref}" in sent
        assert f"gdit:{ref}" not in sent
        assert outbox.get(f"gdit:{ref}") is None
        assert "gdit skip reason=not_at_work" in caplog.text


def test_startup_clears_pending_gdit_when_option_is_off(monkeypatch, tmp_path):
    mod = _load(
        monkeypatch,
        tmp_path,
        agent_token=AGENT,
        mail_to="callcatch-test@proton.me",
        agentmail_inbox="callcatch-test@agentmail.to",
    )
    assert mod.GDIT_WORK_COPY is False
    app = mod.make_app()
    _calls, outbox = mod.ensure_agent(app)
    payload = notify_render.gdit_email(_row())
    outbox.enqueue("gdit", "CC-AB12C", payload)
    outbox.enqueue("email", "CC-AB12C", {"subject": "keep"})
    assert mod.clear_pending_gdit_if_disabled(app) == 1
    assert outbox.get("gdit:CC-AB12C")["status"] == "expired"
    assert outbox.get("email:CC-AB12C")["status"] == "pending"
    assert outbox.get("gdit:CC-AB12C")["payload"] == {}


def test_startup_leaves_pending_gdit_when_option_is_on(monkeypatch, tmp_path):
    mod = _mod(monkeypatch, tmp_path)
    app = mod.make_app()
    _calls, outbox = mod.ensure_agent(app)
    outbox.enqueue("gdit", "CC-AB12C", notify_render.gdit_email(_row()))
    assert mod.clear_pending_gdit_if_disabled(app) == 0
    assert outbox.get("gdit:CC-AB12C")["status"] == "pending"


def test_docs_and_changelog_omit_the_work_address():
    root = pathlib.Path(__file__).resolve().parents[1]
    docs = (root / "DOCS.md").read_text(encoding="utf-8")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert gdit_copy.GDIT_RECIPIENT not in docs
    assert gdit_copy.GDIT_RECIPIENT not in changelog
    assert "gdit.com" not in docs
    assert "gdit.com" not in changelog
