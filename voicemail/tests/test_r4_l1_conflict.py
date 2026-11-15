"""HA-SEC-1 R4-L1: a 409 for a reused Idempotency-Key is permanent; other 409s retry at most 3 times.

Fake data only: invented addresses on reserved test names, no real keys.
"""
from __future__ import annotations

import json
import logging

import pytest


import notify_outbox  # noqa: E402  (path set by test_trust)
import notify_render  # noqa: E402
import notify_send  # noqa: E402

AGENT = "agent-token-555-" + "x" * 24
TO = "callcatch-test@proton.me"
FROM = "callcatch-test@agentmail.to"
KEY = "k" * 40


class _Content:
    def __init__(self, data: bytes, chunk: int = 7):
        self.data = data
        self.chunk = chunk
        self.asked = []

    async def read(self, n=-1):
        self.asked.append(n)
        take = min(n, self.chunk) if n and n > 0 else len(self.data)
        out, self.data = self.data[:take], self.data[take:]
        return out


class _Resp:
    def __init__(self, status, body=b""):
        self.status = status
        self.content = _Content(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _Sess:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        self.calls.append((url, json, headers, kw))
        status, body = self.answers.pop(0)
        return _Resp(status, body)


def _mail(ref="CC-ACDRA"):
    row = {"ref": ref, "created_at": 1_800_000_000, "fields": {"caller_name": "Reuse Test", "reason": "x"}}
    return notify_render.email(row, [TO], FROM)


def _err(code, name="ConflictError", message="Conflict"):
    return json.dumps({"name": name, "code": code, "message": message}).encode()


# --- R4-L1: 409 handling ---------------------------------------------------------

@pytest.mark.parametrize("body", [
    _err("idempotency_conflict"),
    _err("idempotency_key_reused"),
    _err("IDEMPOTENCY_KEY_MISMATCH"),
    _err("", name="IdempotencyKeyReusedError"),
])
@pytest.mark.asyncio
async def test_409_for_reused_key_is_permanent(body, caplog):
    caplog.set_level(logging.DEBUG)
    sess = _Sess([(409, body)])
    send = notify_send.agentmail_sender(lambda: sess, FROM, KEY, TO)
    result = await send({"ref": "CC-ACDRA", "payload": _mail()})
    assert result == (False, "http-409-key-reused", False)
    assert "ConflictError" not in caplog.text and "Conflict" not in caplog.text


@pytest.mark.parametrize("body", [_err("conflict"), b"", b"not json", b"[1,2]", b"\xff\xfe",
                                  json.dumps({"code": 7}).encode()])
@pytest.mark.asyncio
async def test_other_409_stays_retryable(body):
    sess = _Sess([(409, body)])
    send = notify_send.agentmail_sender(lambda: sess, FROM, KEY, TO)
    assert await send({"ref": "CC-ACDRA", "payload": _mail()}) == (False, "http-409", True)


@pytest.mark.asyncio
async def test_error_body_read_is_bounded_and_loops_short_reads():
    big = _err("idempotency_conflict") + b" " * 10_000
    resp = _Resp(409, big)
    data = await notify_send._read_error_body(resp)
    assert len(data) == notify_send.ERROR_BODY_MAX
    assert all(0 < n <= notify_send.ERROR_BODY_MAX for n in resp.content.asked)
    assert notify_send.conflict_is_key_reuse(data[: len(_err("idempotency_conflict"))])

    class Broken:
        status = 409

        class content:  # noqa: N801
            @staticmethod
            async def read(_n):
                raise OSError("reset")

    assert await notify_send._read_error_body(Broken()) == b""
    assert notify_send.conflict_is_key_reuse(b"") is False


def test_conflict_detector_ignores_message_text():
    """Branch on code/name only, as AgentMail's error reference asks."""
    assert not notify_send.conflict_is_key_reuse(_err("conflict", message="key already used for a different request"))
    assert not notify_send.conflict_is_key_reuse(json.dumps({"code": None, "name": None}).encode())


@pytest.mark.asyncio
async def test_outbox_caps_ambiguous_409_retries_at_three(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "notify_outbox.json")
    assert ob.enqueue("email", "CC-ACDRB", _mail("CC-ACDRB"))
    sess = _Sess([(409, _err("conflict"))] * 5)
    senders = {"email": notify_send.agentmail_sender(lambda: sess, FROM, KEY, TO)}
    clock = notify_outbox.now_ts()
    states = []
    for _ in range(5):
        for rec in await ob.pump_once(senders, clock=clock):
            states.append((rec["status"], rec["attempts"], rec["last_code"]))
        clock += notify_outbox.MAX_DELAY_S + 1
    # First try plus 3 retries, then parked.  Nothing after.
    assert states == [("pending", 1, "http-409"), ("pending", 2, "http-409"),
                      ("pending", 3, "http-409"), ("dead", 4, "http-409")]
    assert len(sess.calls) == 4 and ob.counts()["dead"] == 1
    keys = {c[2]["Idempotency-Key"] for c in sess.calls}
    assert keys == {"callcatch-CC-ACDRB"}


@pytest.mark.asyncio
async def test_outbox_parks_reused_key_on_first_answer(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "notify_outbox.json")
    assert ob.enqueue("email", "CC-ACDRC", _mail("CC-ACDRC"))
    sess = _Sess([(409, _err("idempotency_conflict"))])
    senders = {"email": notify_send.agentmail_sender(lambda: sess, FROM, KEY, TO)}
    recs = await ob.pump_once(senders)
    assert [(r["status"], r["attempts"], r["last_code"]) for r in recs] == [("dead", 1, "http-409-key-reused")]
    job = ob.get("email:CC-ACDRC")
    assert KEY not in json.dumps(job) and "idempotency_conflict" not in json.dumps(job)


@pytest.mark.asyncio
async def test_non_409_failures_do_not_use_the_conflict_budget(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "notify_outbox.json")
    assert ob.enqueue("email", "CC-ACDRD", _mail("CC-ACDRD"))
    answers = [(503, b"")] * 5 + [(409, _err("conflict"))] * 2 + [(200, b"{}")]
    sess = _Sess(answers)
    senders = {"email": notify_send.agentmail_sender(lambda: sess, FROM, KEY, TO)}
    clock = notify_outbox.now_ts()
    last = None
    for _ in range(len(answers)):
        for rec in await ob.pump_once(senders, clock=clock):
            last = rec
        clock += notify_outbox.MAX_DELAY_S + 1
    assert last["status"] == "sent" and last["attempts"] == 8
