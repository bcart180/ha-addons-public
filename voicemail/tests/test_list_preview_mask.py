"""0.5.5: list preview never shows an unmasked number. Fake 555 data only."""
import pathlib
import re
import sys

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(APP))

FULL = re.compile(r"\d{5,}|\d{3}[\s.\-)]+\d{3}[\s.\-]+\d{4}")


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("VM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VM_MOCK", "1")
    import server as s  # noqa: PLC0415
    return s


@pytest.mark.parametrize("snippet", [
    "5555550123", "+15555550123", "(555) 555-0123", "555.555.0123", "+1 555-555-0123",
    "Caller: +15555550123", "  555-555-0123  ",
])
def test_number_only_snippet_preview_is_masked(server, snippet):
    item = server.normalize_item({"snippet": snippet, "caller": "+15555550123"})
    assert not FULL.search(item["preview"]), item["preview"]
    assert "0123" in item["preview"] or item["preview"] == ""
    # detail view keeps the full number
    assert "5555550123" in item["caller_digits"]
    assert item["transcript"].strip() and item["snippet"].strip()


def test_empty_reason_uses_masked_fallback(server, monkeypatch):
    monkeypatch.setattr(server.call_summary, "stored_or_extract", lambda raw: {"reason": ""})
    item = server.normalize_item({"snippet": "5555550123", "caller": "+15555550123"})
    assert item["preview"] == "•••0123"


def test_preview_masks_number_inside_speech(server):
    item = server.normalize_item({"snippet": "call me at 555 555 0199", "caller": "+15555550123"})
    assert not FULL.search(item["preview"])


def test_preview_prefers_summary_reason(server):
    item = server.normalize_item({"snippet": "Hi, it's about the furnace filter, call 555-555-0144",
                                  "caller": "+15555550123"})
    if item["summary"].get("reason"):
        assert item["preview"] == item["summary"]["reason"]
    assert not FULL.search(item["preview"])


def test_app_js_list_never_uses_raw_snippet():
    js = (APP / "static" / "app.js").read_text()
    line = next(ln for ln in js.splitlines() if "const preview =" in ln)
    assert "v.preview" in line and "v.snippet" not in line
