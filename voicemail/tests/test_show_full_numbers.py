import call_summary as cs
import inbox_store


def _row():
    return {"voicemail_id": "v1", "caller": "+15555553927", "caller_title": "Yoga Life, +1 (555) 555-3927",
            "transcript": "Call me at 555-555-0142 please", "summary": {"name": "", "reason": "call 555-555-0142", "callback": "15555550142", "urgency": "normal"}}


def test_default_masked(monkeypatch):
    monkeypatch.setattr(cs, "SHOW_FULL_NUMBERS", False)
    r = inbox_store.sensor_state([_row()])["attributes"]["voicemails"][0]
    assert r["caller_masked"] == "•••3927"
    assert r["caller_label_masked"] == "Yoga Life, •••3927"
    assert r["summary_callback"] == "•••0142"


def test_full_numbers_on(monkeypatch):
    monkeypatch.setattr(cs, "SHOW_FULL_NUMBERS", True)
    r = inbox_store.sensor_state([_row()])["attributes"]["voicemails"][0]
    assert r["caller_masked"] == "(555) 555-3927"
    assert r["caller_label_masked"] == "Yoga Life, (555) 555-3927"
    assert r["summary_callback"] == "(555) 555-0142"
    # transcript-derived text stays masked
    assert "•••0142" in r["summary_reason"] and "555-0142" not in r["summary_reason"]
    assert "555-0142" not in r["summary_fallback"]


def test_number_only_title(monkeypatch):
    monkeypatch.setattr(cs, "SHOW_FULL_NUMBERS", True)
    assert cs.caller_label({"caller": "+15555553927", "caller_title": "+1 (555) 555-3927"}) == "(555) 555-3927"
    assert cs.caller_number("12") == "•••"
