"""Dedicated CallCatch-only sender inbox.

Fake data only: invented addresses on reserved test names, no real keys.
"""
from __future__ import annotations

import json
import logging
import pathlib
import re

import pytest

from test_trust import _load

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


# --- dedicated sender inbox ----------------------------------------------------

PINNED = "callcatch@callcatch.brysoncarter.com"


def test_sender_must_be_a_dedicated_callcatch_inbox():
    for ok in ("callcatch@agentmail.to", FROM, "callcatch.notices@agentmail.to", "CallCatch-Test@AgentMail.to",
               PINNED, "CallCatch@CallCatch.BrysonCarter.com"):
        assert notify_send.sender_allowed(ok), ok
    for bad in ("hub@agentmail.to", "owner@agentmail.to", "notices-callcatch@agentmail.to",
                "x.callcatch@agentmail.to", "callcatch@agentmail.to.evil.com", "callcatch@gmail.com",
                "callcatch@example.com", "callcatch%x@agentmail.to", "callcatch test@agentmail.to",
                "callcatch@agentmail.to\nBcc: x@example.com", "callcatch" + "x" * 60 + "@agentmail.to", "",
                "callcatch@brysoncarter.com", "callcatch-notices@callcatch.brysoncarter.com",
                "anyone@callcatch.brysoncarter.com", "callcatch@foo.brysoncarter.com",
                "callcatch@callcatch.brysoncarter.com.evil.com", "notcallcatch@callcatch.brysoncarter.com",
                "callcatch@brysoncarter.com ",
                "*@brysoncarter.com", "callcatch@agentmail.to@callcatch.brysoncarter.com"):
        assert not notify_send.sender_allowed(bad), bad


def test_sender_schema_matches_code():
    cfg = (pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    line = next(x for x in cfg.splitlines() if x.strip().startswith("agentmail_inbox: match("))
    pattern = re.search(r"match\((.*)\)\?\s*$", line).group(1)
    assert pattern == notify_send.SENDER_RE.pattern
    assert re.fullmatch(pattern, FROM) and re.fullmatch(pattern, PINNED)
    assert not re.fullmatch(pattern, "hub@agentmail.to")
    assert not re.fullmatch(pattern, "callcatch@brysoncarter.com")
    assert not re.fullmatch(pattern, "anyone@callcatch.brysoncarter.com")
    assert not re.fullmatch(pattern, "callcatch@foo.brysoncarter.com")


def test_shared_inbox_turns_email_off_without_echoing_it(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="agentmail", mail_to=TO,
              agentmail_inbox="sharedhub@agentmail.to", agentmail_api_key=KEY)
    assert m.AGENTMAIL_INBOX == "" and m.MAIL_FROM == "" and m.mail_ready() is False
    assert "email" not in m.build_senders(m.make_app())
    assert "agentmail_inbox refused" in caplog.text
    assert "sharedhub" not in caplog.text and KEY not in caplog.text
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="agentmail", mail_to=TO,
              agentmail_inbox=FROM, agentmail_api_key=KEY)
    assert m.mail_ready() is True and m.MAIL_FROM == FROM


def test_proton_recipient_pin_unchanged():
    assert notify_send.RECIPIENT_DOMAINS == ("proton.me", "protonmail.com")
    assert notify_send.parse_recipient(TO) == TO
    assert notify_send.parse_recipient(PINNED) == ""
    assert notify_send.parse_recipient("callcatch-test@gmail.com") == ""


def test_mail_ready_requires_the_sender_allowlist(monkeypatch, tmp_path):
    """Fields being set is not enough: the From address has to pass the allowlist."""
    m = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="agentmail", mail_to=TO,
              agentmail_inbox=FROM, agentmail_api_key=KEY)
    assert m.mail_ready() is True
    m.AGENTMAIL_INBOX = "hub@agentmail.to"
    assert m.mail_ready() is False
    m.AGENTMAIL_INBOX = "callcatch@brysoncarter.com"
    assert m.mail_ready() is False
    m.AGENTMAIL_INBOX = PINNED
    assert m.mail_ready() is True
    m.AGENTMAIL_API_KEY = ""
    assert m.mail_ready() is False
    m.AGENTMAIL_API_KEY = KEY
    m.MAIL_BACKEND = "off"
    assert m.mail_ready() is False
    m.MAIL_BACKEND = "agentmail"
    saved = m.MAIL_TO
    m.MAIL_TO = ""
    assert m.mail_ready() is False
    m.MAIL_TO = saved
    assert m.mail_ready() is True
    pinned = _load(monkeypatch, tmp_path, agent_token=AGENT, mail_backend="agentmail", mail_to=TO,
                   agentmail_inbox=PINNED, agentmail_api_key=KEY)
    assert pinned.mail_ready() is True and pinned.MAIL_FROM == PINNED and pinned.AGENTMAIL_INBOX == PINNED


@pytest.mark.asyncio
async def test_body_has_no_reply_to_cc_or_bcc():
    sess = _Sess([(200, b"{}")])
    send = notify_send.agentmail_sender(lambda: sess, FROM, KEY, TO)
    payload = dict(_mail(), reply_to=["x@example.com"], cc=["x@example.com"], bcc=["x@example.com"])
    assert await send({"ref": "CC-ACDRA", "payload": payload}) == (True, "sent", False)
    body = sess.calls[0][1]
    assert set(body) == {"to", "subject", "text", "html", "labels"}
    assert sess.calls[0][0] == f"https://api.agentmail.to/v0/inboxes/{FROM}/messages/send"
