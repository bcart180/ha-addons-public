"""0.9.0 Climate screen: schedule preference is not a hold; override duration is explicit."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

from engine import Engine  # noqa: E402
from server import make_app  # noqa: E402
from test_engine import NOW, FakeHA, ZONES, lt14, opts, states  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "rootfs", "app", "static", "index.html")


@pytest.fixture
def mk(aiohttp_client, monkeypatch):
    async def _mk(o=None, s=None, allow="127.0.0.1"):
        monkeypatch.setenv("SH_ALLOW", allow)
        ha = FakeHA(s or states())
        e = Engine(o or opts(), ha, None, clock=lambda: NOW, localtime=lt14)
        e.evaluate()
        ha.calls.clear()
        return await aiohttp_client(make_app(e, interval=3600)), ha, e
    return _mk


def test_screen_copy_separates_preference_hold_and_auto():
    html = open(INDEX).read()
    assert "Does not start a hold" in html
    assert "Temporary hold" in html
    assert "Full Auto" in html
    assert "Resume automatic" in html
    assert "Fan Auto is a fan speed, not Full Auto." in html
    assert "Synthetic example" in html
    assert "History unavailable" in html
    assert "No energy, cost, or efficiency" in html
    assert "vacation_setback:" in html and "vacation_at_setback:" in html and "vacation_radiant_off:" in html
    assert "http://" not in html and "https://" not in html
    # Boot reads state. It does not post.
    assert 'api("api/state")' in html
    assert "classic" in html and "thermostat" in html


async def test_opening_the_screen_changes_nothing(mk):
    c, ha, e = await mk()
    e.start_override("lr", 2)
    until = e.st["override_until"]["lr"]
    ha.calls.clear()
    r = await c.get("/")
    assert r.status == 200
    assert (await c.get("/api/state")).status == 200
    assert ha.calls == []
    assert e.st["override_until"]["lr"] == until
    assert (await c.get("/classic")).status == 200


async def test_preference_does_not_create_a_hold(mk):
    c, ha, e = await mk()
    j = await (await c.post("/api/zone/lr/preference", json={"profile": "home", "temperature": 70})).json()
    z = next(x for x in j["zones"] if x["name"] == "lr")
    assert z["override_until"] is None
    assert z["preferences"]["home"]["temperature"] == 70
    assert z["preferences"]["home"]["source"] == "zone"
    assert e.st["prefs"]["zones"]["lr"]["home"] == 70
    # The schedule may write the new target. That write is not a hold.
    assert all(c[0] != "set_hvac_mode" or c[1].get("hvac_mode") == "heat" for c in ha.calls)


async def test_preference_while_held_does_not_clear_the_hold_or_rewrite_the_unit(mk):
    c, ha, e = await mk()
    e.start_override("lr", 2)
    until = e.st["override_until"]["lr"]
    ha.calls.clear()
    j = await (await c.post("/api/zone/lr/preference", json={"profile": "home", "temperature": 69})).json()
    z = next(x for x in j["zones"] if x["name"] == "lr")
    assert z["override_until"] == until
    assert not [c for c in ha.calls if c[1].get("entity_id") == "climate.lr"]
    assert z["preferences"]["home"]["temperature"] == 69


async def test_preference_rejects_bad_values_and_strangers(mk):
    c, ha, _ = await mk()
    assert (await c.post("/api/zone/lr/preference", json={"profile": "home", "temperature": 99})).status == 400
    assert (await c.post("/api/zone/lr/preference", json={"profile": "fan", "temperature": 70})).status == 400
    assert (await c.post("/api/zone/nope/preference", json={"profile": "home", "temperature": 70})).status == 404
    assert ha.calls == []


async def test_explicit_override_uses_the_requested_duration(mk):
    c, ha, _ = await mk()
    assert (await c.post("/api/zone/lr/override", json={"temperature": 70})).status == 400
    j = await (await c.post("/api/zone/lr/override", json={"temperature": 70, "hours": 1})).json()
    z = next(x for x in j["zones"] if x["name"] == "lr")
    assert z["override_until"] == NOW + 3600
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 70.0}) in ha.calls
    # The old setpoint path is still a 2 h hold.
    j2 = await (await c.post("/api/zone/lr/setpoint", json={"temperature": 72})).json()
    z2 = next(x for x in j2["zones"] if x["name"] == "lr")
    assert z2["override_until"] == NOW + 2 * 3600


async def test_resume_one_and_both(mk):
    c, _, e = await mk()
    e.start_override("lr", 2)
    e.start_override("bath", 2)
    j = await (await c.post("/api/zone/lr/resume", json={})).json()
    assert next(z for z in j["zones"] if z["name"] == "lr")["override_until"] is None
    assert next(z for z in j["zones"] if z["name"] == "bath")["override_until"]
    j2 = await (await c.post("/api/resume-all", json={})).json()
    assert all(z["override_until"] is None for z in j2["zones"])
    assert j2["profile"] == "auto"


async def test_auto_profile_can_show_away_while_presence_is_home(mk):
    """The screen must be able to show both facts. This test does not explain the combination."""
    c, ha, e = await mk()
    e.st["profile"] = "auto"
    e.st["last_result"] = {"ts": NOW, "presence_home": True, "zones": {
        "lr": {"profile": "away", "reason": "manual_override", "setpoint": 70,
               "action": "hold", "live": {"mode": "heat"}}
    }}
    e.start_override("lr", 2)
    ha.calls.clear()
    j = await (await c.get("/api/state")).json()
    assert j["profile"] == "auto"
    assert j["last"]["presence_home"] is True
    assert j["last"]["zones"]["lr"]["profile"] == "away"
    assert next(z for z in j["zones"] if z["name"] == "lr")["override_until"]
    assert ha.calls == []


async def test_mode_hours_hold_both_splits(mk):
    zones = [dict(ZONES[0]), dict(ZONES[0], name="br", label="Room B", climate="climate.br", indoor="sensor.br_t")]
    zones[1]["heat_only"] = False
    s = states()
    s["climate.br"] = ("cool", {"temperature": 71, "current_temperature": 70, "hvac_modes": ["off", "heat", "cool"]})
    s["sensor.br_t"] = ("70", {})
    c, ha, _ = await mk(opts(zones=zones + [ZONES[1]]), s)
    ha.s["climate.br"] = ("cool", {"temperature": 71, "current_temperature": 70, "hvac_modes": ["off", "heat", "cool"]})
    ha.calls.clear()
    j = await (await c.post("/api/zone/lr/mode", json={"hvac_mode": "heat", "hours": 4})).json()
    until = {z["name"]: z["override_until"] for z in j["zones"]}
    assert until["lr"] == NOW + 4 * 3600
    assert until["br"] == NOW + 4 * 3600
    assert ("set_hvac_mode", {"entity_id": "climate.br", "hvac_mode": "off"}) in ha.calls
    assert ("set_hvac_mode", {"entity_id": "climate.lr", "hvac_mode": "heat"}) in ha.calls
    assert ("set_hvac_mode", {"entity_id": "climate.br", "hvac_mode": "heat"}) in ha.calls


async def test_preference_and_override_are_ingress_only(mk):
    c, _, _ = await mk(allow="172.30.32.2")
    assert (await c.post("/api/zone/lr/preference", json={"profile": "home", "temperature": 70})).status == 403
    assert (await c.post("/api/zone/lr/override", json={"temperature": 70, "hours": 1})).status == 403
    assert (await c.post("/api/resume-all", json={})).status == 403


async def test_history_unavailable_is_502(mk):
    c, ha, _ = await mk()

    def boom(*_a):
        raise OSError("down")
    ha.get_history = boom
    assert (await c.get("/api/zone/lr/history?range=24h")).status == 502


def test_version_and_slug_notes():
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    assert 'version: "0.9.0"' in raw
    card = open(os.path.join(os.path.dirname(__file__), "..", "lovelace", "smart-hvac-card.js")).read()
    assert "a1d3bd4b_smart_hvac" in card and "47b3c4e5_smart_hvac" in card
