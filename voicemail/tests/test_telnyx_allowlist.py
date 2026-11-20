"""0.8.1 connection allowlist: other Telnyx connections are never touched.  Fake data only.

The fake ids below are not real Telnyx connections.  OTHER_CONN stands in for
another app on the same account (for example an outbound-only TeXML number).
Numbers are 555-01xx.  Keys are placeholders.  No live network.
"""
from __future__ import annotations

import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from test_trust import _load
from test_telnyx_capture import (
    AGENT, CALLER, CONN, HDR, OTHER_CONN, REC, SESSION, TELNYX_KEY, URL, XAI_KEY,
    FakeNet, _emails, api_record, saved_event, stamp,
)

import telnyx_capture  # noqa: E402

OTHER_CALLER = "+15555550199"
OTHER_URL = "https://s3.amazonaws.com/telephony-recorder-prod/other.wav?sig=placeholder"
NOCONN_URL = "https://s3.amazonaws.com/telephony-recorder-prod/noconn.wav?sig=placeholder"
OTHER_REC = "22222222-2222-4222-8222-2222222299a1"
NOCONN_REC = "22222222-2222-4222-8222-2222222299a2"
INT_REC = "22222222-2222-4222-8222-2222222299a3"


class CountingNet(FakeNet):
    """FakeNet that also counts speech-to-text calls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stt_calls = 0

    async def stt(self, blob: bytes, fmt: str, channels: str):
        self.stt_calls += 1
        return await super().stt(blob, fmt, channels)


def other_record(rid: str = OTHER_REC, url: str = OTHER_URL, connection=OTHER_CONN) -> dict:
    rec = {
        "id": rid,
        "status": "completed",
        "call_session_id": "11111111-1111-4111-8111-1111111199a1",
        "call_leg_id": "33333333-3333-4333-8333-3333333399a1",
        "channels": "dual",
        "download_urls": {"wav": url},
        "duration_millis": 35000,
        "recording_started_at": stamp(-40),
        "recording_ended_at": stamp(-5),
        "from": OTHER_CALLER,
        "to": "sip:+15555550198@sip.example.invalid;transport=tls",
    }
    if connection is not None:
        rec["connection_id"] = connection
    return rec


def _options(**extra) -> dict:
    base = dict(
        agent_token=AGENT, push_service="mobile_app_test_phone",
        mail_to="callcatch-test@proton.me", agentmail_inbox="callcatch-test@agentmail.to",
        telnyx_api_key=TELNYX_KEY, xai_api_key=XAI_KEY, telnyx_delete_remote=True,
    )
    base.update(extra)
    return base


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("VM_CAPTURE_AUTO", "0")
    return _load(monkeypatch, tmp_path, **_options(telnyx_connection_ids=CONN))


async def _advance(mod, app, net, gate: str = "send", attach=None):
    cap = mod.ensure_capture(app)
    hook = attach or (lambda job: mod.attach_capture(app, job))
    return await cap.advance(net, hook, delete_gate=gate)


# ---- option parsing ---------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("", frozenset()),
    (None, frozenset()),
    ("   ", frozenset()),
    (CONN, frozenset({CONN})),
    (f"{CONN},{OTHER_CONN}", frozenset({CONN, OTHER_CONN})),
    (f" {CONN} , {OTHER_CONN} ", frozenset({CONN, OTHER_CONN})),
    (f"{CONN} {OTHER_CONN}", frozenset({CONN, OTHER_CONN})),
    (f"{CONN},abc", frozenset()),            # one bad token empties the list
    (f"{CONN},-1", frozenset()),
    ("1" * 33, frozenset()),
    (",".join(str(10**18 + n) for n in range(11)), frozenset()),  # more than 10
])
def test_parse_connection_ids_fails_closed(raw, expected):
    assert telnyx_capture.parse_connection_ids(raw) == expected


def test_connection_id_of_accepts_string_or_int_only():
    assert telnyx_capture.connection_id_of(CONN) == CONN
    assert telnyx_capture.connection_id_of(int(CONN)) == CONN
    assert telnyx_capture.connection_id_of(True) == ""
    assert telnyx_capture.connection_id_of(None) == ""
    assert telnyx_capture.connection_id_of("12ab") == ""


def test_empty_allowlist_object_allows_nothing(tmp_path):
    cap = telnyx_capture.Capture(tmp_path)
    assert cap.allowed == frozenset()
    assert cap.connection_allowed(CONN) is False
    bad = telnyx_capture.Capture(tmp_path, allowed_connections={CONN, "not-an-id"})
    assert bad.allowed == frozenset()


def test_capture_off_without_allowlist_even_with_both_keys(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("TELNYX_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("VM_CAPTURE_AUTO", "0")
    caplog.set_level(logging.WARNING)
    blank = _load(monkeypatch, tmp_path, **_options())
    assert blank.CAPTURE_ON is False
    assert any("telnyx_connection_ids is blank or invalid" in r.message for r in caplog.records)
    assert TELNYX_KEY not in " ".join(caplog.messages) and XAI_KEY not in " ".join(caplog.messages)
    caplog.clear()
    invalid = _load(monkeypatch, tmp_path, **_options(telnyx_connection_ids=f"{CONN},abc"))
    assert invalid.CAPTURE_ON is False
    assert invalid.TELNYX_CONNECTION_IDS == frozenset()
    good = _load(monkeypatch, tmp_path, **_options(telnyx_connection_ids=CONN))
    assert good.CAPTURE_ON is True and good.TELNYX_CONNECTION_IDS == frozenset({CONN})


def test_config_schema_has_the_option_and_defaults_blank():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    config = (root / "config.yaml").read_text(encoding="utf-8")
    assert '  telnyx_connection_ids: ""\n' in config
    assert "  telnyx_connection_ids: match(^[0-9, ]{0,400}$)?\n" in config


# ---- poller path --------------------------------------------------------------

@pytest.mark.asyncio
async def test_mixed_poll_touches_only_the_allowlisted_connection(mod, caplog):
    """Worst case: notify gate is 'send' (go-live).  Other connections still get nothing."""
    caplog.set_level(logging.INFO)
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        cap = mod.ensure_capture(app)
        net = CountingNet([{"text": "allowlisted call", "duration": 2}])
        net.records = [
            other_record(),                                            # PhoneZero-style app
            other_record(NOCONN_REC, NOCONN_URL, connection=None),     # no connection id at all
            other_record(INT_REC, OTHER_URL, connection=int(OTHER_CONN)),  # integer id, still other
            api_record(),                                              # the CallCatch connection
        ]
        assert await cap.poll(net) == "poll-ok"
        jobs = cap.jobs()
        assert set(jobs) == {REC}
        assert jobs[REC]["connection_id"] == CONN
        assert cap.skipped == 3
        await _advance(mod, app, net, gate="send")
        await _advance(mod, app, net, gate="send")
        assert net.fetched == [URL]                    # never downloaded
        assert net.stt_calls == 1                      # never transcribed
        assert net.deleted == [REC]                    # never deleted
        emails = _emails(mod, app)
        assert len(emails) == 1                        # never emailed
        mail = json.dumps(next(iter(emails.values())))
        assert "allowlisted call" in mail and "0199" not in mail
        rows = mod.ensure_agent(app)[0].all()
        assert len(rows) == 1
        blob = " ".join(caplog.messages)
        assert OTHER_CONN not in blob and CONN not in blob and OTHER_CALLER not in blob and "placeholder" not in blob


@pytest.mark.asyncio
async def test_poll_is_off_with_an_empty_allowlist(tmp_path):
    cap = telnyx_capture.Capture(tmp_path)
    net = CountingNet([])
    net.records = [api_record(), other_record()]
    assert await cap.poll(net) == "poll-off"
    assert net.listed == [] and cap.jobs() == {}


@pytest.mark.asyncio
async def test_keys_without_allowlist_never_poll_or_ingest(monkeypatch, tmp_path):
    monkeypatch.setenv("VM_CAPTURE_AUTO", "0")
    mod = _load(monkeypatch, tmp_path, **_options())
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        net = CountingNet([{"text": "should never run", "duration": 1}])
        net.records = [api_record(), other_record()]
        await mod.capture_tick(app, net=net, force=True)
        resp = await client.post("/api/agent/telnyx", json={"event": saved_event()}, headers=HDR)
        body = await resp.json()
        assert resp.status == 200 and body["ignored"] is True
        await mod.capture_tick(app, net=net, force=True)
        assert net.listed == [] and net.fetched == [] and net.stt_calls == 0 and net.deleted == []
        assert mod.ensure_capture(app).jobs() == {}
        assert _emails(mod, app) == {}
        diag = mod.agent_diag(app)
        assert diag["capture_on"] is False and diag["telnyx_connections"] == 0
        assert diag["recordings_skipped"] == 1   # the one relayed event, dropped unread


# ---- webhook path -------------------------------------------------------------

@pytest.mark.asyncio
async def test_relayed_events_from_other_or_missing_connection_are_dropped(mod):
    async with TestClient(TestServer(mod.make_app())) as client:
        app = client.server.app
        other = saved_event(event_id="44444444-4444-4444-8444-4444444499b1", url=OTHER_URL,
                            recording_id=OTHER_REC)
        other["connection_id"] = OTHER_CONN
        other["from"] = OTHER_CALLER
        missing = saved_event(event_id="44444444-4444-4444-8444-4444444499b2", url=NOCONN_URL,
                              recording_id=NOCONN_REC)
        missing.pop("connection_id")
        for event in (other, missing):
            resp = await client.post("/api/agent/telnyx", json={"event": event}, headers=HDR)
            body = await resp.json()
            assert resp.status == 200 and body["ignored"] is True and body["duplicate"] is False
        assert mod.ensure_capture(app).jobs() == {}
        net = CountingNet([{"text": "nothing", "duration": 1}])
        await _advance(mod, app, net, gate="send")
        assert net.fetched == [] and net.stt_calls == 0 and net.deleted == []
        assert _emails(mod, app) == {}
        assert mod.ensure_agent(app)[0].all() == []


# ---- stored jobs (defense in depth) -------------------------------------------

def _plant(cap: telnyx_capture.Capture, rid: str, **fields) -> None:
    """Write a job straight into the index, as 0.8.0 (no connection id) would have left it."""
    with telnyx_capture.exclusive_lock(cap.lock_path) if cap.lock_path.exists() else _nullctx():
        data = cap._read()
        job = telnyx_capture._blank_job(rid, telnyx_capture.now_ts())
        job.update(recording_id=rid, call_session_id=SESSION, **fields)
        data["jobs"][rid] = job
        cap._write(data)


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_stored_jobs_from_unlisted_connections_close_as_skipped(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    cap._write(cap._read())
    rid_dl, rid_del, rid_dead, rid_wait = (f"22222222-2222-4222-8222-2222222299c{n}" for n in range(1, 5))
    _plant(cap, rid_dl, state="download", url=URL, url_at=telnyx_capture.now_ts())        # 0.8.0 job, no id
    _plant(cap, rid_del, state="delete", connection_id=OTHER_CONN, transcript="x", ref="CC-AAAAA")
    _plant(cap, rid_dead, state="dead", connection_id=OTHER_CONN, last_error="download")
    _plant(cap, rid_wait, state="wait", connection_id=OTHER_CONN)
    attached: list[dict] = []

    async def attach(job: dict) -> str:
        attached.append(job)
        return "CC-AAAAA"

    net = CountingNet([{"text": "never", "duration": 1}])
    await cap.advance(net, attach, delete_gate="send")
    await cap.advance(net, attach, delete_gate="send")
    jobs = cap.jobs()
    for rid in (rid_dl, rid_del, rid_wait):
        assert jobs[rid]["state"] == "skipped" and jobs[rid]["last_error"] == telnyx_capture.SKIP_ERROR
        assert jobs[rid]["url"] == ""
    assert jobs[rid_dead]["state"] == "dead" and not jobs[rid_dead].get("surfaced")
    assert net.fetched == [] and net.stt_calls == 0 and net.deleted == []
    assert attached == []                     # no row, so no email


@pytest.mark.asyncio
async def test_connection_removed_from_allowlist_stops_before_delete(tmp_path):
    first = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    assert first.ingest_webhook(saved_event())["ignored"] is False
    net = CountingNet([{"text": "first pass", "duration": 2}])

    async def attach(job: dict) -> str:
        return "CC-AAAAA"

    await first.advance(net, attach, delete_gate="dry_run")
    assert first.get(REC)["state"] == "done" and first.get(REC)["deleted"] == "dry_run"
    # Put it back in the delete step, then restart with the connection no longer listed.
    first._update(REC, lambda j: j.update(state="delete", deleted=""))
    later = telnyx_capture.Capture(tmp_path, allowed_connections={OTHER_CONN})
    await later.advance(net, attach, delete_gate="send")
    assert net.deleted == []
    assert later.get(REC)["state"] == "skipped"


@pytest.mark.asyncio
async def test_delete_step_rechecks_the_allowlist(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    cap.ingest_webhook(saved_event())
    net = CountingNet([])
    # The job handed in names another connection.
    job = cap.get(REC)
    job["connection_id"] = OTHER_CONN
    code = await cap._step_delete(REC, job, net, telnyx_capture.now_ts(), delete_gate="send")
    assert code == telnyx_capture.SKIP_ERROR and net.deleted == []
    assert cap.get(REC)["state"] == "skipped"
    # The stored job names another connection.
    cap2 = telnyx_capture.Capture(tmp_path / "b", allowed_connections={CONN})
    cap2.ingest_webhook(saved_event())
    cap2._update(REC, lambda j: j.update(connection_id=OTHER_CONN, state="delete"))
    code = await cap2._step_delete(REC, cap2.get(REC) | {"connection_id": CONN}, net,
                                   telnyx_capture.now_ts(), delete_gate="send")
    assert code == telnyx_capture.SKIP_ERROR and net.deleted == []


def test_integer_connection_id_on_the_list_is_accepted(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    rec = api_record()
    rec["connection_id"] = int(CONN)
    assert cap.ingest_api(rec)["ignored"] is False
    assert cap.get(REC)["connection_id"] == CONN


def test_caller_number_of_skipped_input_is_not_stored(tmp_path):
    cap = telnyx_capture.Capture(tmp_path, allowed_connections={CONN})
    cap.ingest_api(other_record())
    assert not cap.path.exists() or CALLER not in cap.path.read_text() and OTHER_CALLER not in cap.path.read_text()
