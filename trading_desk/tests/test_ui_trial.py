"""UI-4 trial guards. Run: python3 tests/test_ui_trial.py

Synthetic checks only. No broker, no Home Assistant, no secrets.
"""
import asyncio
import hashlib
import os
import pathlib
import re

os.environ.setdefault("TD_OPTIONS", "/nonexistent")

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "rootfs" / "app"
STATIC = APP / "static"
TRIAL = STATIC / "ui-trial"
sys_path = APP
import sys
sys.path.insert(0, str(sys_path))
import server  # noqa: E402

# Production shell, locked so / cannot drift in this change.
PROD = {
    "index.html": ("521205e5b5d2b41152632a10fb8b17de8d2091f9b0f131e49afaa0ce4bf5b80a", 5089),
    "app.js": ("7f0b1a6fec2a20e6adf536b1e4d092f34696803ddec69b3c0c72fd729275eec4", 31283),
    "assets.js": ("bff8b346aeb5015d34b58568a200d67e9619e799d356818a88e7368642a6b65e", 4215),
    "assets.css": ("6bdcfcaa9fb719cec0ae6c24b3b90be308679ef343c666cf3c7d1a09e182467e", 3519),
    "main.js": ("e8e71d9f88edd1bb6e3a6f76fff27abff5bdc2ac92c0887f277fc7ed02beb0b0", 12891),
    "main.css": ("c03c50d03169e2d44e4821cd75524b2c03d33bbd76b637e973826b70408e9043", 4811),
    "style.css": ("ff3e440740e4b8a6348b609d88a5dfda7f6b5f7d1e039c774b9cb76ba16412a3", 10960),
}

SCHEMA_KEYS = [
    "api_key_id", "api_secret_key", "trading_env", "data_feed", "data_stream",
    "status_file", "status_entity", "feed_github_repo", "feed_github_path",
    "feed_github_token", "feed_github_poll_s", "trezor_btc_entity",
    "temp_open_risk_cap_pct", "single_name_cap_pct", "mock", "log_level",
]


def _read(path):
    return path.read_text(encoding="utf-8")


def _amber(r, g, b):
    rf, gf, bf = r / 255, g / 255, b / 255
    hi, lo = max(rf, gf, bf), min(rf, gf, bf)
    if hi == lo:
        return False
    d = hi - lo
    if hi == rf:
        hue = ((gf - bf) / d) % 6
    elif hi == gf:
        hue = (bf - rf) / d + 2
    else:
        hue = (rf - gf) / d + 4
    hue *= 60
    light = (hi + lo) / 2
    sat = d / (1 - abs(2 * light - 1))
    return 15 <= hue <= 52 and sat >= 0.35 and light >= 0.18


def _colors(text):
    found = []
    for m in re.finditer(r"#([0-9a-fA-F]{3,8})\b", text):
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        elif len(h) in (6, 8):
            h = h[:6]
        else:
            continue
        found.append((m.group(0), int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)))
    for m in re.finditer(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", text):
        found.append((m.group(0), int(m.group(1)), int(m.group(2)), int(m.group(3))))
    return found


def test_production_shell_unchanged():
    for name, (digest, size) in PROD.items():
        raw = (STATIC / name).read_bytes()
        assert len(raw) == size, name
        assert hashlib.sha256(raw).hexdigest() == digest, name


def test_version_bump_and_schema():
    cfg = _read(ROOT / "config.yaml")
    assert 'version: "0.3.3"' in cfg
    assert server.VERSION == "0.3.3"
    for key in SCHEMA_KEYS:
        assert re.search(r"^  %s:" % re.escape(key), cfg, re.M), key
    schema = cfg.split("schema:", 1)[1]
    got = re.findall(r"^  ([a-z0-9_]+):", schema, re.M)
    assert got == SCHEMA_KEYS


def test_trial_routes_are_get_only():
    app = server.build_app()
    found = {}
    for route in app.router.routes():
        info = route.resource.get_info()
        path = info.get("path") or info.get("formatter") or info.get("prefix")
        found.setdefault(route.method, set()).add(path)
    assert "/" in found["GET"]
    assert "/ui-trial" in found["GET"]
    assert "/ui-trial/" in found["GET"]
    assert "HEAD" in found or "GET" in found
    for method in found:
        assert method in ("GET", "HEAD"), method
    resp = asyncio.run(server.ui_trial_slash(None))
    assert resp.status == 302
    assert resp.headers["Location"] == "../ui-trial"
    trial = asyncio.run(server.ui_trial(None))
    prod = asyncio.run(server.index(None))
    assert trial._path.name == "index.html"
    assert "ui-trial" in str(trial._path)
    assert prod._path == STATIC / "index.html"


def test_trial_keeps_controls_and_reads():
    html = _read(TRIAL / "index.html")
    main = _read(TRIAL / "main.js")
    app = _read(STATIC / "app.js")
    assets = _read(STATIC / "assets.js")
    for tab in ("main", "desk", "assets"):
        assert 'data-tab="%s"' % tab in html
    assert ">Main</button>" in html and ">Detail</button>" in html and ">Schwab</button>" in html
    for key, label in (("1D", "Day"), ("1W", "Week"), ("1M", "Month"), ("1A", "Year"), ("5A", "5Y")):
        assert '"%s": "%s"' % (key, label) in main
        assert 'data-r="' in main
    assert "disabled" in main and " na" in main and "limited" in main
    assert 'addEventListener("click"' in app and 'tr[data-s]' in app
    assert "api/state" in main and "15000" in main and "document.hidden" in main
    assert "api/events" in app and "45000" in app
    assert "api/bars?symbol=" in app
    assert "api/history?range=" in main
    assert "api/assets" in assets and "60000" in assets
    assert "TDAssets" in main and "TDAssets" in assets
    assert "#detail" in main and "#assets" in main and '"desk"' in main
    assert "Read-only view. This add-on cannot place, change or cancel orders." in html
    assert "READ-ONLY" in html
    assert "tap a position" in html
    for needle in (
        "Everything you own", "gained or lost", "Your stocks and Bitcoin",
        "Trezor Bitcoin", "basis n/a", "Total assets", "Whole portfolio", "Risk vs caps",
        "Day equity", "Long-term holdings (Schwab)", "VIEW-ONLY", "Desk bots",
        "Today's fills", "Rotation proposals", "Open orders", "Thresholds",
        "/share/trading_desk/assets.json",
    ):
        blob = html + main + app + assets
        assert needle in blob, needle


def test_no_orange_and_no_write_verbs():
    html = _read(TRIAL / "index.html")
    assert "style.css" not in html and "main.css" not in html and "assets.css" not in html
    assert "static/ui-trial/main.js" in html
    assert "static/app.js" in html and "static/assets.js" in html
    assert "body.slate #readonly" not in _read(TRIAL / "trial.css")
    assert "#readonly" not in _read(TRIAL / "trial.css")
    files = [TRIAL / "index.html", TRIAL / "trial.css", TRIAL / "main.js", TRIAL / "enhance.js",
             STATIC / "app.js", STATIC / "assets.js", STATIC / "vendor" / "uPlot.min.css"]
    bad = []
    for path in files:
        text = _read(path)
        assert not re.search(r"\b(orange|amber)\b", text, re.I), path.name
        for name, r, g, b in _colors(text):
            if _amber(r, g, b):
                bad.append("%s %s" % (path.name, name))
        assert "c9892b" not in text.lower()
        assert "f59e0b" not in text.lower()
        assert "d4a054" not in text.lower()
    assert bad == [], bad
    css = _read(TRIAL / "trial.css")
    assert "--acc: #3e6ae1" in css or "--acc:#3e6ae1" in css
    assert "--bitcoin:" in css and ".bitcoin" in css
    assert "Schwab" in _read(TRIAL / "main.js") and "Alpaca" in _read(TRIAL / "main.js")
    assert "Bitcoin" in _read(TRIAL / "main.js")


def test_shipped_source_still_clean():
    import selfcheck
    assert selfcheck.scan(APP) == [], selfcheck.scan(APP)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
