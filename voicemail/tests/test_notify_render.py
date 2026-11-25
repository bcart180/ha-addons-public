"""0.8.5 post-call email format.  Fake name, number, and transcript only."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import notify_render  # noqa: E402

TS = 1_795_558_500  # 2026-11-24 17:15:00 America/New_York
FAKE_ROW = {
    "ref": "CC-EXAMPLE",
    "created_at": TS,
    "duration_s": 42,
    "hung_up_early": False,
    "has_audio": True,
    "verbatim_at": TS,
    "summary": "Jordan asked if Saturday afternoon still works for pickup and said he can come by after 3.",
    "transcript": (
        "Assistant: Hi, you've reached the house assistant on a recorded line.  How may I help you?\n"
        "Caller: Hi, this is Jordan Hale.  I wanted to check if Saturday afternoon still works for pickup.\n"
        "Assistant: Saturday after 3 is open.  I'll leave that note for Bryson.\n"
        "Caller: Perfect.  Thank you."
    ),
    "fields": {
        "caller_name": "Jordan Hale",
        "caller_id": "+15550104472",
        "callback_number": "+15550104472",
    },
    "notes": [],
}


def test_subject_names_caller_and_et_time():
    mail = notify_render.email(FAKE_ROW, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    assert mail["subject"] == "Jordan Hale called at 17:15 ET"


def test_header_and_transcript_and_link():
    mail = notify_render.email(
        FAKE_ROW,
        ["callcatch-test@proton.me"],
        "callcatch-test@agentmail.to",
        include_number=False,
        panel_base_url="http://ha.home.arpa:8123",
    )
    text, html = mail["text"], mail["html"]
    assert "Jordan Hale" in text and "Number ending 4472" in text
    assert "Early hangup: No" in text
    assert "42 seconds" in text
    assert "Tue Nov 24, 17:15 ET" in text
    assert "Summary" in text and "Saturday afternoon" in text
    assert "Assistant:" in text and "Caller:" in text
    assert notify_render.UNTRUSTED_NOTE in text and notify_render.UNTRUSTED_NOTE in html
    assert text.index(notify_render.UNTRUSTED_NOTE) < text.index("Transcript")
    assert "The recording is saved in CallCatch." in text
    assert "It is not attached to this email." in text
    assert "http://ha.home.arpa:8123/hassio/ingress/a1d3bd4b_voicemail#CC-EXAMPLE" in text
    assert 'background:#000000' in html and "#3E6AE1" in html
    assert "555-010-4472" not in text and "5550104472" not in html
    assert "(hung up early)" not in mail["subject"]


def test_unknown_caller_falls_back_to_last4():
    row = {
        "ref": "CC-NONAME",
        "created_at": TS,
        "duration_s": 8,
        "hung_up_early": True,
        "fields": {"caller_id": "+15550109999"},
        "transcript": "unlabeled speech",
    }
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to", include_number=False)
    assert mail["subject"] == "Number ending 9999 called at 17:15 ET"
    assert "Early hangup: Yes" in mail["text"]
    assert "Number ending 9999" in mail["text"]
    assert "No recording was saved." in mail["text"]


def test_agent_label_is_shown_as_assistant():
    row = {"ref": "CC-LBL", "created_at": TS, "fields": {"caller_name": "Pat Testcaller"},
           "transcript": "Agent: hello\nCaller: hi"}
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    assert "Assistant" in mail["html"]
    assert "Agent:" not in mail["html"]


def test_cnam_name_is_marked_caller_id():
    row = {
        "ref": "CC-CNAM",
        "created_at": TS,
        "duration_s": 12,
        "fields": {
            "caller_name": "Pat Testcaller",
            "caller_name_source": "cnam",
            "caller_id": "+15550104472",
        },
        "transcript": "unlabeled speech",
    }
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    assert mail["subject"] == "Pat Testcaller (caller ID) called at 17:15 ET"
    assert "Pat Testcaller (caller ID)" in mail["text"]
    assert "Pat Testcaller (caller ID)" in mail["html"]
    note = notify_render.push(row)
    assert note["title"] == "CallCatch: Pat Testcaller (caller ID)"
    contact = dict(FAKE_ROW)
    contact["fields"] = {**FAKE_ROW["fields"], "caller_name_source": "contact"}
    mail = notify_render.email(contact, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to")
    assert mail["subject"] == "Jordan Hale called at 17:15 ET"
    assert "(caller ID)" not in mail["subject"]
    assert "(caller ID)" not in notify_render.push(contact)["title"]


def test_unknown_caller_html_shows_last4_once():
    row = {
        "ref": "CC-NONAME",
        "created_at": TS,
        "duration_s": 8,
        "hung_up_early": True,
        "fields": {"caller_id": "+15550109999"},
        "transcript": "unlabeled speech",
    }
    mail = notify_render.email(row, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to", include_number=False)
    assert mail["text"].count("Number ending 9999") == 1
    assert mail["html"].count("Number ending 9999") == 1
    assert mail["html"].count("<h1") == 1
    # Named callers still get a last-4 subline.
    named = notify_render.email(FAKE_ROW, ["callcatch-test@proton.me"], "callcatch-test@agentmail.to", include_number=False)
    assert "Jordan Hale" in named["html"]
    assert named["html"].count("Number ending 4472") == 1
