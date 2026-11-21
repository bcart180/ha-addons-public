"""Dual-channel STT speaker labels (CallCatch 0.8.4)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import telnyx_capture as cap  # noqa: E402

GREETING = "Hi, you have reached the assistant on a recorded line. How may I help you?"
CALLER = "Yes, I am calling about an appointment."
MERGED = f"{GREETING} {CALLER}"


def _words(text: str, start: float) -> list[dict]:
    words = []
    t = start
    for token in text.split():
        words.append({"text": token, "start": t, "end": t + 0.2})
        t += 0.2
    return words


def _dual(ch0: str, ch1: str, *, merged: str | None = None, with_words: bool = True) -> dict:
    channels = [
        {"index": 0, "text": ch0, "words": _words(ch0, 0.1) if with_words and ch0 else []},
        {"index": 1, "text": ch1, "words": _words(ch1, 4.0) if with_words and ch1 else []},
    ]
    return {"text": merged if merged is not None else " ".join(p for p in (ch0, ch1) if p), "channels": channels}


def test_greeting_on_channel_0_is_assistant_not_caller():
    # The 0.8.3 bug: index 0 was always Caller, so the assistant opening
    # was stored under the wrong label when it landed on channel A.
    out = cap.turns_from_stt(_dual(GREETING, CALLER), "dual")
    assert out == f"Assistant: {GREETING}\nCaller: {CALLER}"
    assert not out.startswith("Caller:")


def test_greeting_on_channel_1_is_still_assistant():
    out = cap.turns_from_stt(_dual(CALLER, GREETING, merged=f"{CALLER} {GREETING}"), "dual")
    assert out == f"Caller: {CALLER}\nAssistant: {GREETING}"


def test_text_only_channels_use_the_same_roles():
    out = cap.turns_from_stt(_dual(GREETING, CALLER, with_words=False), "dual")
    assert out == f"Assistant: {GREETING}\nCaller: {CALLER}"


def test_words_without_channel_text_still_detect_the_greeting():
    result = {
        "text": MERGED,
        "channels": [
            {"index": 0, "text": "", "words": _words(GREETING, 0.1)},
            {"index": 1, "text": "", "words": _words(CALLER, 4.0)},
        ],
    }
    assert cap.turns_from_stt(result, "dual") == f"Assistant: {GREETING}\nCaller: {CALLER}"


def test_one_channel_greeting_is_assistant():
    result = {"text": GREETING, "channels": [{"index": 0, "text": GREETING, "words": []}]}
    assert cap.turns_from_stt(result, "dual") == f"Assistant: {GREETING}"


def test_ambiguous_channels_stay_unlabeled():
    both = "Just checking in about the time."
    result = _dual(both, "I can call back tomorrow.", merged="Just checking in about the time. I can call back tomorrow.")
    out = cap.turns_from_stt(result, "dual")
    assert out == "Just checking in about the time. I can call back tomorrow."
    assert "Caller:" not in out and "Assistant:" not in out


def test_both_channels_look_like_the_opening_stay_unlabeled():
    result = _dual(GREETING, GREETING, merged=GREETING)
    out = cap.turns_from_stt(result, "dual")
    assert out == GREETING
    assert "Caller:" not in out and "Assistant:" not in out


def test_single_channel_job_is_unlabeled_even_with_a_channels_list():
    out = cap.turns_from_stt(_dual(GREETING, CALLER), "single")
    assert out == MERGED
    assert "Caller:" not in out and "Assistant:" not in out


def test_missing_channels_list_is_unlabeled():
    out = cap.turns_from_stt({"text": MERGED}, "dual")
    assert out == MERGED
    assert "Caller:" not in out


def test_one_channel_without_an_opening_is_unlabeled():
    result = {"text": CALLER, "channels": [{"index": 0, "text": CALLER, "words": []}]}
    out = cap.turns_from_stt(result, "dual")
    assert out == CALLER
    assert "Caller:" not in out and "Assistant:" not in out


def test_unlabeled_fallback_joins_words_when_merged_text_is_missing():
    result = {
        "text": "",
        "channels": [
            {"index": 0, "text": "Hello there.", "words": _words("Hello there.", 0.1)},
            {"index": 1, "text": "Calling back.", "words": _words("Calling back.", 2.0)},
        ],
    }
    out = cap.turns_from_stt(result, "dual")
    assert out == "Hello there. Calling back."
    assert "Caller:" not in out and "Assistant:" not in out


def test_interleave_keeps_roles_when_speakers_alternate():
    greeting_words = _words(GREETING, 0.1)
    caller_words = _words(CALLER, 4.0)
    later = _words("I can take that down.", 8.0)
    result = {
        "text": f"{GREETING} {CALLER} I can take that down.",
        "channels": [
            {"index": 0, "text": f"{GREETING} I can take that down.", "words": greeting_words + later},
            {"index": 1, "text": CALLER, "words": caller_words},
        ],
    }
    out = cap.turns_from_stt(result, "dual")
    assert out == (
        f"Assistant: {GREETING}\n"
        f"Caller: {CALLER}\n"
        "Assistant: I can take that down."
    )
