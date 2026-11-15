"""HA-SEC-1 R4-I1: an expiry count is never dropped when a same-second notice exists.

Fake data only: invented addresses on reserved test names, no real keys.
"""
from __future__ import annotations

import json



import notify_outbox  # noqa: E402  (path set by test_trust)
import notify_render  # noqa: E402

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


# --- R4-I1: expiry count never dropped -------------------------------------------

def test_expiry_count_kept_when_same_second_notice_exists(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "notify_outbox.json")
    t = 1_800_000_000.25
    first = ob.note_expired(2, clock=t)
    assert first == "push_notice:EX-1800000000"
    ob.suppress_pending(clock=t, status="dry_run")  # the first notice closes (switch on)
    # Same second, two more expiries: a second job, not a dropped count.
    second = ob.note_expired(3, clock=t + 0.5)
    assert second == "push_notice:EX-1800000001"
    job = ob.get(second)
    assert job["status"] == "pending" and job["count"] == 3
    assert job["payload"] == notify_outbox.expired_push(3)
    # And again: the next free second.
    ob.suppress_pending(clock=t, status="dry_run")
    assert ob.note_expired(1, clock=t) == "push_notice:EX-1800000002"


def test_expiry_count_still_folds_into_a_pending_notice(tmp_path):
    ob = notify_outbox.Outbox(tmp_path / "notify_outbox.json")
    t = 1_800_000_000
    jid = ob.note_expired(1, clock=t)
    assert ob.note_expired(4, clock=t + 30) == jid
    assert ob.get(jid)["count"] == 5
    assert ob.counts()["pending"] == 1
