"""CallCatch normalize: display speaker labels, stored records unchanged.

Run: python3 voicemail/tests/test_normalize.py
"""
import hashlib
import importlib.util
import os
import pathlib
import re
import sys

# SHA-256 of digit-normalized national numbers (non-digits removed, and a
# leading country code 1 dropped so +1 and 10-digit forms share one digest)
# and of lowercase phrases. Plaintext stays out of this file.
_BANNED_SHA256 = frozenset(
    {
        "REMOVED_DIGEST",
        "REMOVED_DIGEST",
        "REMOVED_DIGEST",
        "REMOVED_DIGEST",
        "REMOVED_DIGEST",
    }
)
# NANP with an optional leading +1. Area and exchange are NXX (N = 2-9).
_NANP_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]*)?\(?[2-9]\d{2}\)?[\s.\-]*[2-9]\d{2}[\s.\-]*\d{4}(?!\d)"
)
# Same shape, but the exchange is not the fictional 555 exchange.
_NON_555_NANP_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.\-]*)?\(?[2-9]\d{2}\)?[\s.\-]*(?!555)[2-9]\d{2}[\s.\-]*\d{4}(?!\d)"
)


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _national_digits(raw):
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _phrase_candidates(text):
    words = re.findall(r"[a-z]+", text.lower())
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def _assert_fixture_clean(text):
    for phrase in _phrase_candidates(text):
        assert _sha256(phrase) not in _BANNED_SHA256, "fixture contains a banned phrase"
    for match in _NANP_RE.finditer(text):
        digits = _national_digits(match.group(0))
        if not digits:
            continue
        assert _sha256(digits) not in _BANNED_SHA256, "fixture contains a banned phone number"
    non_555 = _NON_555_NANP_RE.search(text)
    assert non_555 is None, f"non-555 NANP number in fixture: {non_555.group(0)}"


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "rootfs" / "app"

os.environ["VM_OPTIONS"] = "/nonexistent/options.json"
os.environ["VM_MOCK"] = "1"
# Import under a private name so a previous "server" module cannot leak in.
sys.path.insert(0, str(APP))
spec = importlib.util.spec_from_file_location("callcatch_server", APP / "server.py")
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)

assert (APP / "server.py").is_file()
assert (APP / "static" / "index.html").is_file()
assert (APP / "static" / "app.js").is_file()
assert (APP / "static" / "soft-slate.css").is_file()
example_path = APP / "example_voicemails.json"
assert example_path.is_file()
example = example_path.read_text()
assert _NON_555_NANP_RE.search("999-234-5678")
assert _NON_555_NANP_RE.search("+1 (999) 234-5678")
assert _NON_555_NANP_RE.search("1-999-234-5678")
assert _NON_555_NANP_RE.search("9992345678")
assert _NON_555_NANP_RE.search("+1 (555) 555-0101") is None
assert _NON_555_NANP_RE.search("208-555-0100") is None
assert _NON_555_NANP_RE.search("555-0101") is None
assert _sha256(_national_digits("+1 (999) 234-5678")) == _sha256("9992345678")
assert _sha256("9992345678") not in _BANNED_SHA256
assert _sha256("example town") in {_sha256(p) for p in _phrase_candidates("See Example Town")}
_assert_fixture_clean(example)
assert "555-0101" in example and "555-0102" in example and "555-0103" in example and "555-0104" in example

config = (ROOT / "config.yaml").read_text()
assert "slug: voicemail" in config
assert 'version: "0.4.2"' in config
assert "image:" not in config
assert mod.VERSION == "0.4.2"

GREETING = "House: Hi, you've reached the house. This is the assistant. How may I help you?"
MAPPED = "Assistant: Hi, you've reached the house. This is the assistant. How may I help you?"

assert mod.map_speaker_line(GREETING) == MAPPED
assert mod.map_speaker_line("house: hello") == "Assistant: hello"
assert mod.map_speaker_line("HOUSE: hello") == "Assistant: hello"
assert mod.map_speaker_line("assistant: hello") == "Assistant: hello"
assert mod.map_speaker_line("Assistant: hello") == "Assistant: hello"
assert mod.map_speaker_line("agent:hello") == "Assistant: hello"
assert mod.map_speaker_line("Agent: hello") == "Assistant: hello"
assert mod.map_speaker_line("caller: Hi this is Sam") == "Caller: Hi this is Sam"
assert mod.map_speaker_line("Caller: Hi") == "Caller: Hi"
assert mod.map_speaker_line("CALLER: Hi") == "Caller: Hi"
assert mod.map_speaker_line("House:") == "Assistant:"
assert mod.map_speaker_line("  House: hi") == "  Assistant: hi"
assert mod.map_speaker_line("Household: stays") == "Household: stays"
assert mod.map_speaker_line("This is his assistant. How may I help you?") == (
    "This is his assistant. How may I help you?"
)
assert mod.map_speaker_line("Clinic: Appointment") == "Clinic: Appointment"
assert mod.map_speaker_line("Source: twilio_house_voice") == "Source: twilio_house_voice"
assert mod.map_speaker_text("caller: Hi\r\nHouse: Hello\r\n") == "Caller: Hi\r\nAssistant: Hello\r\n"
assert mod.map_speaker_text("") == ""
assert mod.map_speaker_text(None) is None

raw = {
    "voicemail_id": "v1",
    "caller": "12085550100",
    "caller_title": "Unknown",
    "snippet": "House: Hi, you've reached the house.",
    "transcript": "caller: Hello\nHouse: Hi, you've reached the house. This is the assistant.\nagent: Okay",
    "heard": False,
}
item = mod.normalize_item(raw)
assert raw["transcript"].startswith("caller: Hello\nHouse:")
assert raw["snippet"].startswith("House:")
assert item["snippet"] == "Assistant: Hi, you've reached the house."
assert item["transcript"] == (
    "Caller: Hello\n"
    "Assistant: Hi, you've reached the house. This is the assistant.\n"
    "Assistant: Okay"
)
assert item["voicemail_id"] == "v1"
assert item["caller_title"] == "Unknown"

long_snippet = "House: " + ("x" * 200)
long_item = mod.normalize_item({"snippet": long_snippet, "caller": "1"})
assert long_item["snippet"].startswith("Assistant: ")
assert "House:" not in long_item["snippet"]
assert len(long_item["snippet"]) == 180 - len("House: ") + len("Assistant: ")
assert long_snippet.startswith("House: ")

print("ok normalize")
