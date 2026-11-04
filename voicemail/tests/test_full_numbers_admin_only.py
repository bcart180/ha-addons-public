"""Full caller numbers: admin-only panel only; shared sensor/push last 4 only."""
import pathlib
import re

import inbox_store
import server

CONFIG = (pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text()


def _row():
    return {"voicemail_id": "v1", "caller": "+15555550142", "caller_pretty": "(555) 555-0142",
            "caller_title": "Pat, +1 (555) 555-0142", "transcript": "Call me at 555-555-0177 please",
            "summary": {"name": "Pat", "reason": "call 555-555-0177", "callback": "15555550177", "urgency": "normal"}}


def test_panel_is_admin_only():
    assert re.search(r"^panel_admin: true$", CONFIG, re.M)
    assert "show_full_numbers" not in CONFIG


def test_shared_sensor_last4_only():
    r = inbox_store.sensor_state([_row()])["attributes"]["voicemails"][0]
    blob = repr(r)
    assert r["caller_masked"] == "•••0142"
    assert r["caller_label_masked"] == "Pat, •••0142"
    assert r["summary_callback"] == "•••0177"
    assert not re.search(r"555\D{0,3}01\d\d", blob), blob


def test_admin_panel_detail_full():
    item = server.normalize_item(_row())
    assert "555-0142" in item["caller_title"]
    assert item["summary"]["callback_display"] == "(555) 555-0177"
