"""No orange or amber on the Smart HVAC card or panels (UI rule: pure black, accent #3E6AE1, no orange/amber)."""
import colorsys
import os
import re

ROOT = os.path.join(os.path.dirname(__file__), "..")
FILES = ("lovelace/smart-hvac-card.js", "rootfs/app/static/index.html", "rootfs/app/static/thermostat.html",
         "rootfs/app/static/classic.html")
COLOR = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def _warm(match):
    if match.group(1):
        h = match.group(1)
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        r, g, b = (int(h[k:k + 2], 16) for k in (0, 2, 4))
    else:
        r, g, b = (int(x) for x in match.group(2, 3, 4))
    hue, light, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return 15 <= hue * 360 <= 60 and sat > 0.25 and 0.15 < light < 0.95


def test_no_orange_or_amber_anywhere_on_the_card_or_panels():
    hits = []
    for rel in FILES:
        for n, line in enumerate(open(os.path.join(ROOT, rel), encoding="utf-8"), 1):
            hits += [(rel, n, m.group(0)) for m in COLOR.finditer(line) if _warm(m)]
            if re.search(r"\b(orange|amber)\b", line, re.I):
                hits.append((rel, n, "named"))
    assert hits == []


def test_hold_color_matches_the_panel_hold_token():
    card = open(os.path.join(ROOT, FILES[0]), encoding="utf-8").read()
    index = open(os.path.join(ROOT, FILES[1]), encoding="utf-8").read()
    assert 'hold: "#f2f2f2"' in card and ".gd.h{color:#f2f2f2}" in card
    assert "--hold:#f2f2f2" in index
    assert "#d9902b" not in card
