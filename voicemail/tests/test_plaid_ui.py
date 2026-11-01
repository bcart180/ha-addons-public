"""0.3.3 Plaid theme guards: pure black, no branding in the chrome, touch-first."""
from __future__ import annotations

import pathlib
import re

STATIC = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_canvas_is_pure_black():
    assert re.search(r"--bg:\s*#000\b", CSS)
    assert '<meta name="theme-color" content="#000000"' in HTML
    assert "soft-slate.css" not in HTML


def test_no_branding_in_visible_chrome():
    assert "<h1>CallCatch</h1>" not in HTML
    assert "<title>CallCatch</title>" not in HTML
    assert "through CallCatch" not in JS


def test_touch_targets_at_least_48px():
    m = re.search(r"--tap:\s*(\d+)px", CSS)
    assert m and int(m.group(1)) >= 48
    assert re.search(r"\.icon-btn\.play\s*\{[^}]*width:\s*(7\d|[89]\d)px", CSS)


def test_wide_screens_split_list_and_detail():
    assert "@media (min-width: 900px)" in CSS
    assert "grid-template-columns: minmax(340px, 400px) 1fr" in CSS
    assert "has-detail" in JS and "numbers-open" in JS


def test_opening_detail_does_not_happen_on_load():
    # Opening a voicemail marks it heard, so the wide layout must never auto-open one.
    assert "openDetail(0)" not in JS
