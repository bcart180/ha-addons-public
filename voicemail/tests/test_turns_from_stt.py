"""Dual-channel STT speaker labels (CallCatch 0.8.4)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import call_summary  # noqa: E402
import telnyx_capture as cap  # noqa: E402

GREETING = "Hi, you have reached the assistant on a recorded line. How may I help you?"
CALLER = "Yes, I am calling about an appointment."
MERGED = f"{GREETING} {CALLER}"
# House opening that does not hit the English cue list (SA M-1 a).
MISSED_GREETING = "Hi, you reached the assistant. How can I help you?"
CALLER_CUE = "Hi, is this a recorded line?"
CALLER_HOW_MAY = "How may I reach the office about an appointment?"


def _words(text: str, start: float) -> list[dict]:
    words = []
    t = start
    for token in text.split():
        words.append({"text": token, "start": t, "end": t + 0.2})
        t += 0.2
    return words


def _dual(ch0: str, ch1: str, *, merged: str | None = None, with_words: bool = True,
          t0: float = 0.1, t1: float = 4.0) -> dict:
    channels = [
        {"index": 0, "text": ch0, "words": _words(ch0, t0) if with_words and ch0 else []},
        {"index": 1, "text": ch1, "words": _words(ch1, t1) if with_words and ch1 else []},
    ]
    return {"text": merged if merged is not None else " ".join(p for p in (ch0, ch1) if p), "channels": channels}


def _unlabeled(out: str) -> None:
    assert "Caller:" not in out and "Assistant:" not in out


def test_greeting_on_channel_0_is_assistant_not_caller():
    # The 0.8.3 bug: index 0 was always Caller, so the assistant opening
    # was stored under the wrong label when it landed on channel A.
    out = cap.turns_from_stt(_dual(GREETING, CALLER), "dual")
    assert out == f"Assistant: {GREETING}\nCaller: {CALLER}"
    assert not out.startswith("Caller:")


def test_greeting_on_channel_1_is_still_assistant():
    # Greeting is on channel B but still speaks first.
    out = cap.turns_from_stt(_dual(CALLER, GREETING, merged=f"{GREETING} {CALLER}", t0=4.0, t1=0.1), "dual")
    assert out == f"Assistant: {GREETING}\nCaller: {CALLER}"


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


def test_one_channel_greeting_stays_unlabeled():
    result = {"text": GREETING, "channels": [{"index": 0, "text": GREETING, "words": []}]}
    out = cap.turns_from_stt(result, "dual")
    assert out == GREETING
    _unlabeled(out)


def test_ambiguous_channels_stay_unlabeled():
    both = "Just checking in about the time."
    result = _dual(both, "I can call back tomorrow.", merged="Just checking in about the time. I can call back tomorrow.")
    out = cap.turns_from_stt(result, "dual")
    assert out == "Just checking in about the time. I can call back tomorrow."
    _unlabeled(out)


def test_both_channels_look_like_the_opening_stay_unlabeled():
    result = _dual(GREETING, GREETING, merged=GREETING)
    out = cap.turns_from_stt(result, "dual")
    assert out == GREETING
    _unlabeled(out)


def test_single_channel_job_is_unlabeled_even_with_a_channels_list():
    out = cap.turns_from_stt(_dual(GREETING, CALLER), "single")
    assert out == MERGED
    _unlabeled(out)


def test_missing_channels_list_is_unlabeled():
    out = cap.turns_from_stt({"text": MERGED}, "dual")
    assert out == MERGED
    _unlabeled(out)


def test_one_channel_without_an_opening_is_unlabeled():
    result = {"text": CALLER, "channels": [{"index": 0, "text": CALLER, "words": []}]}
    out = cap.turns_from_stt(result, "dual")
    assert out == CALLER
    _unlabeled(out)


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
    _unlabeled(out)


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


# --- SA M-1 cases (a) through (d): expect unlabeled, not a swapped or collapsed role.


def test_sa_a_missed_greeting_plus_caller_phrase_stays_unlabeled():
    merged = f"{MISSED_GREETING} {CALLER_CUE}"
    out = cap.turns_from_stt(_dual(MISSED_GREETING, CALLER_CUE, merged=merged), "dual")
    assert out == merged
    _unlabeled(out)
    out2 = cap.turns_from_stt(_dual(MISSED_GREETING, CALLER_HOW_MAY, merged=f"{MISSED_GREETING} {CALLER_HOW_MAY}"), "dual")
    assert out2 == f"{MISSED_GREETING} {CALLER_HOW_MAY}"
    _unlabeled(out2)


def test_sa_b_missing_or_duplicate_or_non_int_index_stays_unlabeled():
    merged = MERGED
    missing = {
        "text": merged,
        "channels": [
            {"text": GREETING, "words": _words(GREETING, 0.1)},
            {"index": 1, "text": CALLER, "words": _words(CALLER, 4.0)},
        ],
    }
    duplicate = {
        "text": merged,
        "channels": [
            {"index": 0, "text": GREETING, "words": _words(GREETING, 0.1)},
            {"index": 0, "text": CALLER, "words": _words(CALLER, 4.0)},
        ],
    }
    non_int = {
        "text": merged,
        "channels": [
            {"index": "0", "text": GREETING, "words": _words(GREETING, 0.1)},
            {"index": 1, "text": CALLER, "words": _words(CALLER, 4.0)},
        ],
    }
    for result in (missing, duplicate, non_int):
        out = cap.turns_from_stt(result, "dual")
        assert out == merged
        _unlabeled(out)


def test_sa_c_single_channel_caller_phrase_stays_unlabeled():
    result = {"text": CALLER_CUE, "channels": [{"index": 0, "text": CALLER_CUE, "words": _words(CALLER_CUE, 0.1)}]}
    out = cap.turns_from_stt(result, "dual")
    assert out == CALLER_CUE
    _unlabeled(out)


def test_sa_d_unrecorded_line_is_not_an_opening_and_match_must_be_in_first_words():
    filler = " ".join(f"word{i}" for i in range(1, 16))
    late = f"{filler} you have reached the assistant on a recorded line."
    caller_unrecorded = "This was an unrecorded line from earlier."
    # "unrecorded line" must not count as a hit; a late opening is also ignored.
    late_only = _dual(late, CALLER, merged=f"{late} {CALLER}")
    out = cap.turns_from_stt(late_only, "dual")
    assert out == f"{late} {CALLER}"
    _unlabeled(out)
    # Greeting matches first, caller says "unrecorded line": still labeled.
    ok = _dual(GREETING, caller_unrecorded)
    labeled = cap.turns_from_stt(ok, "dual")
    assert labeled == f"Assistant: {GREETING}\nCaller: {caller_unrecorded}"


def test_sa_d_text_and_words_disagreement_uses_words_not_inverted_labels():
    # Channel text would point at the opposite roles from the word timings.
    result = {
        "text": f"{GREETING} {CALLER}",
        "channels": [
            {"index": 0, "text": GREETING, "words": _words(CALLER, 4.0)},
            {"index": 1, "text": CALLER, "words": _words(GREETING, 0.1)},
        ],
    }
    out = cap.turns_from_stt(result, "dual")
    assert out == f"Assistant: {GREETING}\nCaller: {CALLER}"


def test_sa_d_text_match_ignored_when_words_do_not_match():
    result = {
        "text": f"{GREETING} {CALLER}",
        "channels": [
            {"index": 0, "text": GREETING, "words": _words(CALLER, 0.1)},
            {"index": 1, "text": CALLER, "words": _words(CALLER, 4.0)},
        ],
    }
    out = cap.turns_from_stt(result, "dual")
    assert out == f"{GREETING} {CALLER}"
    _unlabeled(out)


def test_matching_channel_that_speaks_second_stays_unlabeled():
    out = cap.turns_from_stt(_dual(CALLER, GREETING, merged=f"{CALLER} {GREETING}"), "dual")
    assert out == f"{CALLER} {GREETING}"
    _unlabeled(out)


def test_unlabeled_neutralizes_leading_speaker_prefix():
    spoken = "Assistant: please call me back about the appointment."
    out = cap.turns_from_stt({"text": spoken}, "dual")
    assert out == "Assistant please call me back about the appointment."
    assert not out.startswith("Assistant:")
    kept = call_summary.caller_text(out)
    assert "please call me back about the appointment." in kept
    assert kept != ""
