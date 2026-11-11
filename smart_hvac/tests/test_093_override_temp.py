"""0.9.3: one remembered override temperature per room; a hold uses it and pick_mode."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import core  # noqa: E402
from engine import Engine  # noqa: E402
from server import make_app  # noqa: E402
from test_060_mode_lock import StampedHA  # noqa: E402
from test_060_mode_lock import opts as lock_opts  # noqa: E402
from test_060_mode_lock import split_modes, st  # noqa: E402
from test_core import LR, NOW, RAD, base  # noqa: E402
from test_engine import FakeHA, eng, lt14, opts, states  # noqa: E402

INDEX = os.path.join(os.path.dirname(__file__), "..", "rootfs", "app", "static", "index.html")
CFG = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def test_unset_hold_still_freezes_like_092():
    assert core.decide(base(override_active=True), LR)["reason"] == "manual_override"
    assert core.decide(base(override_active=True), LR)["action"] == "hold"


def test_room_below_override_target_heats():
    r = core.decide(base(override_active=True, override_temp=71, indoor_temp=69, outdoor_temp=80,
                         last_mode="cool", last_setpoint=78), LR)
    assert r["mode"] == "heat" and r["setpoint"] == 71.0 and r["action"] == "set"


def test_room_above_override_target_cools():
    r = core.decide(base(override_active=True, override_temp=71, indoor_temp=73, outdoor_temp=80,
                         last_mode="heat", last_setpoint=65), LR)
    assert r["mode"] == "cool" and r["setpoint"] == 71.0


# Lockout is lowered so these two tests see the deadband and the season tie-break, not the season guard.
_OPEN = {"cool_lockout": 20}


def test_inside_deadband_keeps_current_mode():
    r = core.decide(base(override_active=True, override_temp=71, indoor_temp=70, outdoor_temp=63,
                         last_mode="cool", last_setpoint=71), LR, _OPEN)
    assert r["action"] == "hold" and r["mode"] == "cool" and r["reason"] == "manual_override"


def test_inside_deadband_uses_season_tiebreak():
    r = core.decide(base(override_active=True, override_temp=71, indoor_temp=71, outdoor_temp=50,
                         last_mode="cool", last_setpoint=71), LR, _OPEN)
    assert r["mode"] == "heat"


def test_short_cycle_blocks_then_allows_the_flip():
    blocked = core.decide(base(override_active=True, override_temp=71, indoor_temp=76, outdoor_temp=85,
                               last_mode="heat", last_setpoint=71, last_mode_change_ts=NOW - 300), LR)
    assert blocked["action"] == "hold" and blocked["reason"] == "short_cycle_guard" and blocked["mode"] == "heat"
    ready = core.decide(base(override_active=True, override_temp=71, indoor_temp=76, outdoor_temp=85,
                             last_mode="heat", last_setpoint=71, last_mode_change_ts=NOW - 1000), LR)
    assert ready["mode"] == "cool" and ready["action"] == "set"


def test_cool_lockout_forces_heat_during_a_hold():
    outdoor = core.decide(base(override_active=True, override_temp=70, indoor_temp=80, outdoor_temp=40,
                               last_mode="cool", last_setpoint=70), LR)
    assert outdoor["mode"] == "heat"
    forecast = core.decide(base(override_active=True, override_temp=70, indoor_temp=80, outdoor_temp=50,
                                forecast_high=60, last_mode="cool", last_setpoint=70), LR)
    assert forecast["mode"] == "heat"
    open_season = core.decide(base(override_active=True, override_temp=70, indoor_temp=80, outdoor_temp=85,
                                   last_mode="heat", last_setpoint=70), LR)
    assert open_season["mode"] == "cool"


def test_person_off_and_missing_sensor_still_hold():
    off = core.decide(base(override_active=True, override_temp=71, last_mode="off", indoor_temp=60), LR)
    assert (off["action"], off["reason"], off["mode"]) == ("hold", "unit_off", "off")
    missing = core.decide(base(override_active=True, override_temp=71, indoor_temp=None), LR)
    assert missing["action"] == "hold" and missing["reason"] == "missing:indoor_temp"
    outdoor = core.decide(base(override_active=True, override_temp=71, outdoor_temp=None), LR)
    assert outdoor["reason"] == "missing:outdoor_temp"


def test_defrost_does_not_chase_an_override_setpoint():
    r = core.decide(base(override_active=True, override_temp=71, indoor_temp=66, outdoor_temp=50,
                         last_mode="heat", last_setpoint=65, defrosting=True), LR)
    assert r["action"] == "hold" and r["reason"] == "defrosting" and r["mode"] == "heat"


def test_heat_only_zone_never_requests_cool():
    r = core.decide(base(override_active=True, override_temp=70, indoor_temp=90, outdoor_temp=90,
                         last_mode="cool", last_setpoint=70), RAD)
    assert r["mode"] == "heat" and r["mode"] != "cool"


def test_solar_and_humidity_do_not_move_an_override_target():
    sun = core.decide(base(override_active=True, override_temp=71, indoor_temp=66, outdoor_temp=50,
                           sun_elev=45, sun_az=225, cloud_pct=0, last_mode="heat", last_setpoint=65), LR)
    scheduled = core.decide(base(sun_elev=45, sun_az=225, cloud_pct=0), LR)
    assert scheduled["setpoint"] < 71.0
    assert sun["setpoint"] == 71.0 and sun["mode"] == "heat"
    humid = core.decide(base(override_active=True, override_temp=71, indoor_temp=76, outdoor_temp=85,
                             humidity=70, last_mode="cool", last_setpoint=78), LR)
    scheduled_cool = core.decide(base(indoor_temp=76, outdoor_temp=85, humidity=70, last_mode="cool",
                                      last_setpoint=78), LR)
    assert scheduled_cool["setpoint"] == 70.5
    assert humid["mode"] == "cool" and humid["setpoint"] == 71.0


def test_override_result_has_no_heat_cool_pair():
    r = core.decide(base(override_active=True, override_temp=70, indoor_temp=66, outdoor_temp=50,
                         last_mode="heat", last_setpoint=70), LR)
    assert set(r) <= {"action", "mode", "setpoint", "reason", "profile"}
    assert "away_heat" not in r and "away_cool" not in r


def test_shared_mode_lock_during_a_hold():
    ha = StampedHA(st(lr=("heat", 70, 60), br=("heat", 70, 80), outdoor=80))
    e = Engine(lock_opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.set_override_temp("lr", 70)
    e.set_override_temp("br", 70)
    e.start_override("lr", 2)
    e.start_override("br", 2)
    ha.calls.clear()
    r = e.evaluate()
    assert r["br"]["reason"] == "mode_lock" and r["br"]["mode"] == "heat"
    assert not core.shared_mode_conflict([r["lr"]["mode"], r["br"]["mode"]])
    assert not core.shared_mode_conflict(split_modes(ha))


def test_radiant_hold_stays_heat():
    # Below the radiant air shutoff, so this is the mode picker and not the over-temp cut.
    s = states(**{"sensor.bath_t": ("78", {}), "weather.w": ("sunny", {"temperature": 90, "cloud_coverage": 100})})
    e, ha = eng(s=s)
    e.set_override_temp("bath", 70)
    e.start_override("bath", 2)
    ha.calls.clear()
    r = e.evaluate()
    assert r["bath"]["mode"] == "heat"
    assert not any(c[1].get("hvac_mode") == "cool" for c in ha.calls)


def test_saving_a_temperature_does_not_hold_or_write():
    e, ha = eng()
    e.evaluate()
    ha.calls.clear()
    e.set_override_temp("lr", 69)
    assert ha.calls == []
    assert not e._override_active("lr", NOW)
    assert e.saved_override_temp("lr") == 69.0
    assert (e.st.get("prefs") or {}).get("zones", {}).get("lr", {}).get("home") is None


def test_clamps_reject_out_of_range():
    e, _ = eng()
    with pytest.raises(ValueError):
        e.set_override_temp("lr", 54)
    with pytest.raises(ValueError):
        e.set_override_temp("lr", 84.5)
    e.set_override_temp("lr", 84)
    assert e.saved_override_temp("lr") == 84.0
    with pytest.raises(ValueError):
        e.set_override_temp("bath", 86)
    e.opts["radiant_setpoint_max"] = 80
    with pytest.raises(ValueError):
        e.set_override_temp("bath", 81)
    assert e.set_override_temp("bath", 80) == 80.0
    with pytest.raises(ValueError):
        e.set_override_temp("nope", 70)


def test_remembered_value_survives_resume_expiry_and_restart(tmp_path):
    p = str(tmp_path / "state.json")
    t = [NOW]
    e, _ = eng(tmp=p, clock=lambda: t[0])
    e.set_override_temp("lr", 73)
    e.start_override("lr", 1)
    e.resume("lr")
    assert e.saved_override_temp("lr") == 73.0
    assert not e.st["override_until"].get("lr")
    e.start_override("lr", 1)
    t[0] += 3600 + 5
    e.evaluate()
    assert "lr" not in e.st["override_until"]
    assert e.saved_override_temp("lr") == 73.0
    e2, _ = eng(tmp=p, clock=lambda: t[0])
    assert e2.saved_override_temp("lr") == 73.0
    assert "override_temps" in e2.st


def test_ha_change_remembers_the_temperature_not_the_preference():
    t = [NOW]
    e, ha = eng(clock=lambda: t[0])
    e.evaluate()
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75))
    t[0] += 300
    ha.calls.clear()
    r = e.evaluate()
    assert r["lr"]["reason"] == "manual_override" and ha.calls == []
    assert e.saved_override_temp("lr") == 75.0
    prefs = (e.st.get("prefs") or {}).get("zones", {}).get("lr", {})
    assert "home" not in prefs and "sleep" not in prefs


def test_state_file_keeps_prefs_and_sanitizes_override_temps(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({
        "profile": "auto",
        "prefs": {"global": {"home": 71}, "zones": {"lr": {"home": 70, "sleep": 68, "extra": 9}}},
        "override_temps": {"lr": 72, "bath": "nope", "flag": True, "bad": None},
    }))
    e, _ = eng(tmp=str(p))
    assert e.st["prefs"]["zones"]["lr"]["home"] == 70.0
    assert e.st["prefs"]["zones"]["lr"]["sleep"] == 68.0
    assert "extra" not in e.st["prefs"]["zones"]["lr"]
    assert e.st["prefs"]["global"]["home"] == 71.0
    assert e.st["override_temps"] == {"lr": 72.0}
    missing = tmp_path / "old.json"
    missing.write_text(json.dumps({"profile": "sleep", "prefs": {"global": {}, "zones": {"lr": {"home": 69}}}}))
    e2, _ = eng(tmp=str(missing))
    assert "override_temps" not in e2.st
    assert e2.st["prefs"]["zones"]["lr"]["home"] == 69.0


def test_state_view_is_one_temperature_or_unset():
    e, _ = eng()
    view = e.override_temp_view(e.zone("lr"))
    assert view == {"temperature": None, "source": "unset"}
    assert "mode" not in view and "heat" not in view and "cool" not in view
    e.set_override_temp("lr", 70.2)
    view = e.snapshot()["zones"][0]["override_temps"]
    assert view == {"temperature": 70.0, "source": "zone"}
    assert e.snapshot()["override_temps"]["lr"] == view
    assert "mode" not in view


def test_screen_drops_heat_cool_and_header_links():
    html = open(INDEX).read()
    assert "Earlier layout" not in html and "Thermostat cards" not in html
    assert ".links" not in html
    assert "Hold both in Heat" not in html and "Hold both in Cool" not in html
    assert "Off · both" in html
    assert "Heat and cool are chosen automatically from the temperature." in html
    assert "Manual override " in html and "Auto " in html and "America/New_York" in html and " ET" in html
    draft = 'savedOv != null ? savedOv : (held && live.setpoint != null ? live.setpoint : (pt != null ? pt : 70))'
    assert draft in html
    assert "override-temp" in html
    assert 'version: "0.9.4"' in open(CFG).read()
    raw = open(CFG).read()
    assert "override_temp" not in raw.split("schema:", 1)[0]
    assert "override_temp" not in raw.split("schema:", 1)[1]


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


async def test_override_temp_saves_without_a_hold_or_a_write(mk):
    c, ha, e = await mk()
    e.start_override("lr", 2)
    until = e.st["override_until"]["lr"]
    ha.calls.clear()
    j = await (await c.post("/api/zone/lr/override-temp", json={"temperature": 70, "hvac_mode": "cool"})).json()
    z = next(x for x in j["zones"] if x["name"] == "lr")
    assert z["override_temps"] == {"temperature": 70.0, "source": "zone"}
    assert "mode" not in z["override_temps"]
    assert z["override_until"] == until
    assert ha.calls == []
    assert e.st["override_until"]["lr"] == until


async def test_override_temp_rejects_a_pair_and_a_bad_zone(mk):
    c, ha, e = await mk()
    assert (await c.post("/api/zone/nope/override-temp", json={"temperature": 70})).status == 400
    assert (await c.post("/api/zone/lr/override-temp", json={"heat": 70, "cool": 74})).status == 400
    assert (await c.post("/api/zone/lr/override-temp", json={"temperature": {"heat": 70, "cool": 74}})).status == 400
    assert (await c.post("/api/zone/lr/override-temp", json={"temperature": 54})).status == 400
    assert (await c.post("/api/zone/lr/override-temp", json={"temperature": 85})).status == 400
    assert (await c.post("/api/zone/bath/override-temp", json={"temperature": 86})).status == 400
    assert (await c.post("/api/zone/bath/override-temp", json={"temperature": 85})).status == 200
    assert ha.calls == []
    assert not e.st["override_until"].get("lr")


async def test_override_temp_allowed_when_off_or_practicing(mk):
    c, ha, _ = await mk(opts(enabled=False, dry_run=False))
    assert (await c.post("/api/zone/lr/override-temp", json={"temperature": 68})).status == 200
    assert ha.calls == []
    c2, ha2, _ = await mk(opts(dry_run=True))
    assert (await c2.post("/api/zone/lr/override-temp", json={"temperature": 68})).status == 200
    assert ha2.calls == []


async def test_override_temp_is_ingress_only(mk):
    c, _, _ = await mk(allow="172.30.32.2")
    assert (await c.post("/api/zone/lr/override-temp", json={"temperature": 70})).status == 403


def _hot_room(ha):
    ha.s["sensor.lr_t"] = ("80", {})
    ha.s["weather.w"] = ("sunny", {"temperature": 85, "cloud_coverage": 100})
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=71, current_temperature=80))
    ha.calls.clear()


async def test_starting_override_selects_mode_now(mk):
    c, ha, e = await mk()
    _hot_room(ha)
    body = {"temperature": 70, "hours": 1, "hvac_mode": "heat"}
    j = await (await c.post("/api/zone/lr/override", json=body)).json()
    assert ("set_hvac_mode", {"entity_id": "climate.lr", "hvac_mode": "cool"}) in ha.calls
    assert e.saved_override_temp("lr") == 70.0
    assert next(z for z in j["zones"] if z["name"] == "lr")["override_until"] == NOW + 3600


async def test_starting_setpoint_selects_mode_now(mk):
    c, ha, e = await mk()
    _hot_room(ha)
    await c.post("/api/zone/lr/setpoint", json={"temperature": 68, "hvac_mode": "heat"})
    assert ("set_hvac_mode", {"entity_id": "climate.lr", "hvac_mode": "cool"}) in ha.calls
    assert e.saved_override_temp("lr") == 68.0


async def test_classic_and_thermostat_routes_stay(mk):
    c, _, _ = await mk()
    assert (await c.get("/classic")).status == 200
    assert (await c.get("/thermostat")).status == 200
    assert (await c.get("/")).status == 200
