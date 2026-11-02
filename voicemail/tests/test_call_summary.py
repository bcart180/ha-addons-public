"""0.3.7 post-call structured summary. Synthetic 555-01xx data only."""
from __future__ import annotations

import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rootfs" / "app"))

import call_summary as cs  # noqa: E402
import inbox_store  # noqa: E402

LABELLED = (
    "Assistant: Hi, you've reached the house. Who's calling?\n"
    "Caller: Hi, this is Dana Reyes from Brightline Plumbing. I'm calling about the water heater "
    "install tomorrow, there's a leak at the valve so it's urgent. Call me back at 555-555-0142.\n"
    "Assistant: Thanks, I'll pass that on."
)


def test_labelled_transcript_uses_caller_lines_only():
    s = cs.extract_summary(LABELLED, "+15555550111")
    assert s["name"] == "Dana Reyes"
    assert s["reason"].startswith("The water heater install tomorrow")
    assert s["callback"] == "5555550142"  # the number the caller asked for
    assert s["urgency"] == "urgent"
    assert s["source"] == "local"
    assert "pass that on" not in s["reason"]


def test_low_urgency_and_caller_number_fallback():
    s = cs.extract_summary("Hey it's Sam. No rush, just checking in about dinner Saturday.", "+1 555-555-0123")
    assert s["name"] == "Sam"
    assert s["urgency"] == "low"
    assert s["callback"] == "15555550123"
    assert "dinner Saturday" in s["reason"]


def test_normal_and_no_name():
    s = cs.extract_summary("Calling to confirm the delivery window for Thursday.", "5555550104")
    assert s["name"] == ""
    assert s["urgency"] == "normal"
    assert s["reason"].lower().startswith("confirm the delivery window")


def test_empty_transcript():
    s = cs.extract_summary("", "")
    assert s == {"name": "", "reason": "", "callback": "", "urgency": "normal", "source": "local"}


def test_voice_summary_is_validated():
    raw = {"caller_name": "Dana <b>Reyes</b>", "reason": "Valve leak, call 555-555-0142", "callback": "5555550142", "urgency": "URGENT"}
    s = cs.sanitize_summary(raw, LABELLED, "+15555550111")
    assert s["source"] == "voice"
    assert "<" not in s["name"]
    assert s["urgency"] == "urgent"
    assert s["callback"] == "5555550142"
    assert "0142" in s["reason"] and "555-555-0142" not in s["reason"]  # masked in the reason text


def test_voice_summary_invented_number_dropped():
    raw = {"name": "Pat", "reason": "Quote", "callback": "5555550199", "urgency": "panic"}
    s = cs.sanitize_summary(raw, "Caller: hi it's Pat about a quote", "+15555550105")
    assert s["callback"] == ""  # never spoken and not the caller
    assert s["urgency"] == "normal"
    assert cs.sanitize_summary({"callback": "5555550105"}, "", "") is None
    assert cs.sanitize_summary("nope", "", "") is None


def test_masking_matches_pyscript_style():
    assert cs.mask_number("+1 (555) 555-0199") == "•••0199"
    assert cs.mask_number("Unknown") == "Unknown"
    assert cs.mask_phone_runs("call 555-555-0142 x12 now") == "call •••0142 now"


def test_ingest_stores_summary_and_sensor_is_masked():
    item = inbox_store.item_from_payload(
        {"voicemail_id": "s1", "from": "+15555550111", "transcript_text": LABELLED, "date": "2026-11-01T10:00:00-05:00"}
    )
    assert item["summary"]["urgency"] == "urgent"
    row = inbox_store.sensor_state([item])["attributes"]["recent_items"][0]
    assert row["urgency"] == "urgent"
    assert row["summary_name"] == "Dana Reyes"
    assert row["summary_callback"] == "•••0142"
    assert not re.search(r"\d{3}[-) ]\d{3}-\d{4}", row["summary_reason"] + row["summary_callback"])


def test_ingest_prefers_valid_voice_summary():
    item = inbox_store.item_from_payload(
        {
            "voicemail_id": "s2",
            "from": "+15555550106",
            "transcript_text": "Caller: this is Lee about the gutter quote",
            "summary": {"name": "Lee", "reason": "Gutter quote", "urgency": "low"},
        }
    )
    assert item["summary"]["source"] == "voice"
    assert item["summary"]["callback"] == "15555550106"


def test_old_rows_derive_summary_and_display_adds_tel():
    s = cs.stored_or_extract({"caller": "+15555550107", "transcript": "This is Ari. Please call back today if you can."})
    d = cs.for_display(s)
    assert d["urgency"] == "urgent"
    assert d["callback_tel"] == "tel:+15555550107"
    assert d["callback_display"] == "(555) 555-0107"


_HOUSE_WORD_DIGESTS = (
    (6, "REMOVED_DIGEST"),
    (10, "REMOVED_DIGEST"),
    (8, "REMOVED_DIGEST"),
    (6, "REMOVED_DIGEST"),
    (7, "REMOVED_DIGEST"),
)


def test_no_house_specific_strings():
    src = (ROOT / "rootfs" / "app" / "call_summary.py").read_text(encoding="utf-8").lower()
    # Deny-list of owner/site-specific words, stored as SHA-256 digests so the
    # public repo does not spell them out. Every substring of a listed length is checked.
    for length, digest in _HOUSE_WORD_DIGESTS:
        for i in range(len(src) - length + 1):
            assert hashlib.sha256(src[i : i + length].encode()).hexdigest() != digest


def test_ui_and_blueprint_wired():
    static = ROOT / "rootfs" / "app" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    js = (static / "app.js").read_text(encoding="utf-8")
    bp = (ROOT / "blueprints" / "callcatch_new_voicemail.yaml").read_text(encoding="utf-8")
    assert 'id="d-summary"' in html and "renderSummary(v)" in js
    assert "textContent" in js.split("function renderSummary")[1].split("function renderList")[0]
    assert "summary_callback" in bp and 'message: "{{ push_message }}"' in bp


# ---- PR #52 MA fixes: M1 (Cf chars), L1 (callback length), L2 (markup) ----
import pytest  # noqa: E402

HIDDEN = {
    "zwsp": "\u200b",
    "zwj": "\u200d",
    "rlo": "\u202e",
    "lro": "\u202d",
    "shy": "\u00ad",
}
FULL = re.compile(r"\d{3}\D{0,3}\d{3}\D{0,3}\d{4}")


def _sensor_row(summary: dict, transcript: str = "") -> dict:
    item = inbox_store.item_from_payload(
        {"voicemail_id": "h1", "from": "+15555550111", "transcript_text": transcript or "Caller: hello", "summary": summary}
    )
    return inbox_store.sensor_state([item])["attributes"]["recent_items"][0]


def _bp_vars():
    jinja2 = pytest.importorskip("jinja2")
    import yaml

    class L(yaml.SafeLoader):
        pass

    L.add_constructor("!input", lambda loader, node: None)
    bp = yaml.load((ROOT / "blueprints" / "callcatch_new_voicemail.yaml").read_text(encoding="utf-8"), Loader=L)
    v = bp["variables"]
    env = jinja2.Environment()
    return env, v


def _render_push(row: dict) -> tuple[str, str]:
    env, v = _bp_vars()
    ctx = {"newest": row}
    for key in ("urgency", "summary_line"):
        ctx[key] = env.from_string(v[key]).render(**ctx).strip()
    ctx["first_line"] = "fallback"
    push = env.from_string(v["push_message"]).render(**ctx).strip()
    logbook = f"Would notify: Caller - {push}"  # same push_message the dry-run logbook prints
    return push, logbook


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_format_chars_cannot_hide_a_number(name):
    h = HIDDEN[name]
    num = f"555{h}555{h}0177"
    transcript = f"Caller: this is Kim. Call {num} about the gate."
    row = _sensor_row({"name": f"Kim{h}", "reason": f"call {num} please", "callback": f"555{h}5550177", "urgency": "urgent"}, transcript)
    for field in ("summary_name", "summary_reason", "summary_callback"):
        assert h not in row[field]
        assert not FULL.search(row[field]), (field, row[field])
    assert row["summary_callback"] == "•••0177"
    assert "•••0177" in row["summary_reason"]
    push, logbook = _render_push(row)
    for text in (push, logbook):
        assert h not in text and not FULL.search(text)
    # local extractor path too
    local = cs.extract_summary(transcript, "+15555550111")
    assert h not in local["reason"] and not FULL.search(cs.for_sensor(local)["summary_reason"])


def test_cf_strip_covers_named_chars():
    for h in HIDDEN.values():
        assert cs.strip_format(f"a{h}b") == "ab"
    assert cs.mask_phone_runs("540\u200b555\u200b0177") == "•••0177"


def test_callback_must_be_7_to_15_digits():
    spoken = "Caller: reach me at 555-555-0177"
    long_cb = "9" * 40 + "5555550177"
    assert cs.sanitize_summary({"name": "A", "callback": long_cb}, spoken, "")["callback"] == ""
    assert cs.sanitize_summary({"name": "A", "callback": "12345"}, "Caller: 12345", "")["callback"] == ""
    assert cs.sanitize_summary({"name": "A", "callback": "5555550177"}, spoken, "")["callback"] == "5555550177"
    assert cs.stored_or_extract({"summary": {"urgency": "low", "callback": long_cb}})["callback"] == ""
    assert cs.stored_or_extract({"summary": {"urgency": "low", "callback": "5550177"}})["callback"] == "5550177"
    assert cs.valid_callback("1" * 15) and not cs.valid_callback("1" * 16) and not cs.valid_callback("123456")


def test_markup_is_neutralized_in_push():
    row = _sensor_row(
        {
            "name": "**Boss** <b>Lee</b>",
            "reason": "Open [your invoice](https://evil.example/x) or see https://evil.example now `code` _x_ # hi",
            "urgency": "normal",
        }
    )
    for field in ("summary_name", "summary_reason"):
        assert not re.search(r"[\[\]<>*`_#]|https?:|\]\(", row[field]), row[field]
    assert "your invoice" in row["summary_reason"]
    push, logbook = _render_push(row)
    for text in (push, logbook):
        assert "https:" not in text and "](" not in text and "<b>" not in text and "**" not in text


# ---- PR #52 MA: fallback push (no summary) is masked too ----
def _render_fallback_push(row: dict) -> tuple[str, str]:
    env, v = _bp_vars()
    ctx = {"newest": row}
    for key in ("first_line", "urgency", "summary_line"):
        ctx[key] = env.from_string(v[key]).render(**ctx).strip()
    push = env.from_string(v["push_message"]).render(**ctx).strip()
    return push, f"Would notify: Caller - {push}"


def _no_summary_row(transcript: str) -> dict:
    item = inbox_store.item_from_payload({"voicemail_id": "f1", "from": "", "transcript_text": transcript})
    row = inbox_store.sensor_state([item])["attributes"]["recent_items"][0]
    row["summary_reason"] = ""  # force the no-summary branch of the blueprint
    return row


@pytest.mark.parametrize("hidden", ["", *sorted(HIDDEN.values())])
def test_fallback_line_is_masked_in_push_and_logbook(hidden):
    num = f"555{hidden}-01{hidden}23"
    row = _no_summary_row(f"Hi, call me on 555-555-{hidden}0123 or {num} later\nsecond line")
    assert hidden == "" or hidden not in row["summary_fallback"]
    assert "•••0123" in row["summary_fallback"]
    push, logbook = _render_fallback_push(row)
    for text in (push, logbook):
        assert "•••0123" in text, text
        assert not re.search(r"\d{5,}|\d{3}\D{0,3}\d{2}", text.replace("•••0123", "")), text
        assert hidden == "" or hidden not in text
        assert "second line" not in text


def test_fallback_markup_cannot_join_digits():
    row = _no_summary_row("Caller: dial 555*01*45 or [5550145](https://evil.example)")
    push, _ = _render_fallback_push(row)
    assert "https" not in push and "](" not in push
    assert not re.search(r"\d{5,}", push.replace("•••0145", "")), push


def test_fallback_never_uses_raw_snippet():
    env, v = _bp_vars()
    out = env.from_string(v["first_line"]).render(newest={"snippet": "555-555-0199", "transcript": "555-555-0199"}).strip()
    assert out == "New voicemail"
    assert "snippet" not in v["first_line"] and "transcript" not in v["first_line"]


# ---- PR #52 MA re-gate: M1 separators/fillers/digits, push title ----
SEPS = ["\u2013", "\u2014", "\u2212", "\u2010", "\u2022", "\u00b7", ",", ":", ";", "'", "+", "=", "\uff3f", "\u3001"]
INVIS = ["\u034f", "\u115f", "\u3164", "\u2800", "\ufe0f", "\U000e0100", "\u0301", "\u20dd"]
FULLW = "".maketrans("0123456789", "０１２３４５６７８９")
ARAB = "".maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def _variants():
    for sep in SEPS + INVIS:
        yield f"555{sep}555{sep}0142"
    yield "555-555-0142".translate(FULLW)
    yield "555-555-0142".translate(ARAB)
    yield "555–555–0142".translate(ARAB)
    yield "555\u3164555\u31640142".translate(FULLW)


ANY_FULL = re.compile(r"\d\W{0,3}\d\W{0,3}\d\W{0,3}\d\W{0,3}\d")


@pytest.mark.parametrize("num", list(_variants()))
def test_every_path_masks_to_last4(num):
    transcript = f"Caller: this is Kim. Please call {num} about the gate."
    voice = _sensor_row({"name": "Kim", "reason": f"call {num} please", "urgency": "normal"}, transcript)
    local = cs.for_sensor(cs.extract_summary(transcript, ""))
    saved = cs.for_sensor(cs.stored_or_extract({"summary": {"name": f"Kim {num}", "reason": f"ring {num}", "urgency": "low"}}))
    for row in (voice, local, saved):
        for field in ("summary_name", "summary_reason"):
            assert not ANY_FULL.search(cs.strip_format(row[field]).replace("•••0142", "")), (field, row[field])
    assert "•••0142" in voice["summary_reason"] and "•••0142" in local["summary_reason"]
    assert "•••0142" in saved["summary_reason"]
    push, logbook = _render_push(voice)
    for text in (push, logbook):
        assert not ANY_FULL.search(cs.strip_format(text).replace("•••0142", "")), text
    fb = _no_summary_row(f"Call {num} back")
    push, logbook = _render_fallback_push(fb)
    for text in (push, logbook):
        assert "•••0142" in text and not ANY_FULL.search(text.replace("•••0142", "")), text


def test_strip_format_drops_fillers_and_normalizes_digits():
    for ch in INVIS:
        assert cs.strip_format(f"a{ch}b") == "ab", hex(ord(ch[0]))
    assert cs.strip_format("０１٢٣") == "0123"
    assert cs.mask_phone_runs("٥٥٥٠١٤٢٥٥٥") .endswith("•••2555")


def test_image_link_leaves_no_paren_residue():
    assert cs.plain_text("see ![img](javascript:alert(1)) now") == "see img now"


def _title(row: dict) -> str:
    env, v = _bp_vars()
    ctx = {"newest": row, "caller": row.get("caller", "")}
    label = env.from_string(v["caller_label"]).render(**ctx).strip()
    return label


@pytest.mark.parametrize("payload,expect", [
    ({"from": "+15555550142"}, "•••0142"),
    ({"from": "+15555550142", "caller_title": "+1 (555) 555-0142"}, "•••0142"),
    ({"from": "+15555550142", "caller_title": "Kim Lee"}, "Kim Lee"),
    ({"from": ""}, "Unknown"),  # add-on label for a withheld number
])
def test_push_title_is_masked(payload, expect):
    item = inbox_store.item_from_payload({"voicemail_id": "t1", "transcript_text": "Caller: hi", **payload})
    item.update({k: v for k, v in payload.items() if k == "caller_title"})
    row = inbox_store.sensor_state([item])["attributes"]["recent_items"][0]
    label = _title(row)
    assert label == expect
    assert "5555550142" not in cs.digits(label)


# ---- PR #52 MA re-gate 2: wide separators, words/lookalikes, masked sensor ----
WIDE = [
    "555   555    0142",
    "555\t\t555 \t 0142",
    "555----555----0142",
    "555-.-.-555-.-.-0142",
    "555 x 555 x 0142",
    "555 and 555 then 0142",
    "555<b></b>555<b></b>0142",
    "555-555-O142",
    "555-555-Ol42",
    "SSS-555-0142".replace("SSS", "555"),
    "555 – – 555 — — 0142",
]


@pytest.mark.parametrize("num", WIDE)
def test_wide_separators_mask_on_every_path(num):
    transcript = f"Caller: this is Kim. Please call {num} about the gate."
    voice = _sensor_row({"name": "Kim", "reason": f"call {num} please", "urgency": "normal"}, transcript)
    local = cs.for_sensor(cs.extract_summary(transcript, ""))
    saved = cs.for_sensor(cs.stored_or_extract({"summary": {"name": "Kim", "reason": f"ring {num}", "urgency": "low"}}))
    for row in (voice, local, saved):
        assert "•••0142" in row["summary_reason"], row["summary_reason"]
        rest = row["summary_reason"].replace("•••0142", "")
        assert not re.search(r"555", rest), row["summary_reason"]
    for text in _render_push(voice):
        assert "•••0142" in text and "555" not in text.replace("•••0142", ""), text
    fb = _no_summary_row(f"Call {num} back")
    assert "•••0142" in fb["summary_fallback"]
    for text in _render_fallback_push(fb):
        assert "•••0142" in text and "555" not in text.replace("•••0142", ""), text


def test_lookalikes_only_inside_number_tokens():
    assert cs.mask_phone_runs("Hello Lola, call 555-555-O142") == "Hello Lola, call •••0142"
    assert cs.mask_phone_runs("Ollie IOU 2 lol") == "Ollie IOU 2 lol"


def test_sensor_publishes_masked_fields_only():
    item = inbox_store.item_from_payload(
        {"voicemail_id": "s1", "from": "+15555550142", "transcript_text": "Caller: call me at 555-555-0142\nthanks"}
    )
    item["caller_title"] = "+1 (555) 555-0142"
    attrs = inbox_store.sensor_state([item])["attributes"]
    for row in (attrs["recent_items"][0], attrs["voicemails"][0]):
        for raw in ("caller", "caller_pretty", "caller_title", "snippet", "transcript"):
            assert raw not in row
        blob = repr({k: v for k, v in row.items() if k.startswith(("summary_", "caller"))})
        assert not re.search(r"\d\W{0,3}\d\W{0,3}\d\W{0,3}\d\W{0,3}\d", blob.replace("•••0142", "")), blob
        assert row["caller_masked"] == "•••0142" and row["caller_label_masked"] == "•••0142"
        assert "•••0142" in row["summary_fallback"]


def test_blueprint_without_raw_caller_has_no_number_actions():
    item = inbox_store.item_from_payload({"voicemail_id": "s2", "from": "+15555550142", "transcript_text": "Caller: hi"})
    row = inbox_store.sensor_state([item])["attributes"]["recent_items"][0]
    env, v = _bp_vars()
    caller = env.from_string(v["caller"]).render(newest=row).strip()
    assert caller == ""
    assert "5550142" not in caller


def test_readme_documents_recorder_exclude():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "recorder:" in text and "exclude:" in text and "- sensor.inbound_voicemails" in text
