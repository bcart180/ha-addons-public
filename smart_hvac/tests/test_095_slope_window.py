"""0.9.5 slope open-window pause. Sensors are the generic fixtures (climate.lr, sensor.lr_t, ...)."""
import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import core  # noqa: E402
import defrost_scenario  # noqa: E402
from engine import Engine  # noqa: E402
from test_040 import Clock  # noqa: E402
from test_engine import NOW, lt14  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")
CYCLE = 60
LOOKBACK = 20 * 60
DELAY = 120

SPLITS = [
    {"name": "lr", "label": "Room A", "climate": "climate.lr", "indoor": "sensor.lr_t",
     "facing_az": 180, "solar_factor": 0, "heat_only": False, "enabled": True},
    {"name": "br", "label": "Room B", "climate": "climate.br", "indoor": "sensor.br_t",
     "facing_az": 90, "solar_factor": 0, "heat_only": False, "enabled": True},
]
BATH = {"name": "bath", "label": "Bath", "climate": "climate.bath", "indoor": "sensor.bath_t",
        "facing_az": 90, "solar_factor": 0, "heat_only": True, "enabled": True, "comfort": 72}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat()


class HA:
    def __init__(self, states):
        self.s = states
        self.calls = []
        self.changed = {}
        self.reported = {}

    def get_states(self):
        out = []
        for k, v in self.s.items():
            row = {"entity_id": k, "state": v[0], "attributes": v[1]}
            if k in self.changed:
                row["last_changed"] = self.changed[k]
            if k in self.reported:
                row["last_reported"] = self.reported[k]
            out.append(row)
        return out

    def call(self, domain, service, data):
        self.calls.append((service, data))
        st, at = self.s[data["entity_id"]]
        if service == "set_hvac_mode":
            self.s[data["entity_id"]] = (data["hvac_mode"], at)
        else:
            self.s[data["entity_id"]] = (st, dict(at, temperature=data.get("temperature", at.get("temperature"))))


def opts(**kw):
    zones = kw.pop("zones", None)
    if zones is None:
        zones = [dict(z) for z in SPLITS]
    o = {"enabled": True, "dry_run": False, "window_slope": True, "presence_entity": "device_tracker.phone",
         "weather_entity": "weather.w", "min_cycle_min": 15, "override_hours": 2,
         "profiles": {"home": 71, "guest": 71, "sleep": 68, "away_heat": 65, "away_cool": 78},
         "zones": zones}
    o.update(kw)
    return o


def _modes():
    return ["off", "heat", "cool", "fan_only"]


def world(indoor=70, outdoor=40, zones=None, path=None, **kw):
    s = {
        "device_tracker.phone": ("home", {}),
        "weather.w": ("cloudy", {"temperature": outdoor, "cloud_coverage": 100}),
        "sun.sun": ("below_horizon", {"elevation": -10, "azimuth": 0}),
        "climate.lr": ("heat", {"temperature": 71, "current_temperature": indoor, "hvac_modes": _modes()}),
        "sensor.lr_t": (str(indoor), {}),
        "climate.br": ("heat", {"temperature": 71, "current_temperature": indoor, "hvac_modes": _modes()}),
        "sensor.br_t": (str(indoor), {}),
        "climate.bath": ("heat", {"temperature": 72, "current_temperature": 70, "hvac_modes": ["heat", "off"]}),
        "sensor.bath_t": ("70", {}),
    }
    ha, clk = HA(s), Clock()
    clk.t = NOW
    ha.changed["sensor.lr_t"] = iso(clk.t - 3600)
    ha.changed["sensor.br_t"] = iso(clk.t - 3600)
    ha.changed["sensor.bath_t"] = iso(clk.t - 3600)
    e = Engine(opts(zones=zones, **kw), ha, path, clock=clk, localtime=lt14)
    return e, ha, clk


def settle(e):
    e.evaluate()
    return e


def put(ha, clk, entity, value, stamp=None):
    ha.s[entity] = (str(value), {})
    ha.changed[entity] = iso(clk.t if stamp is None else stamp)


def drive(e, ha, clk, rate, hours, start=70.0, cycle=CYCLE, entity="sensor.lr_t", record=None):
    """1 F steps of a constant slope. Returns the clock time of the first slope pause, or None.

    record, when a list, receives every service call from the whole ramp, not only the last cycle.
    """
    t0, reported, hit = clk.t, float(start), None
    while clk.t < t0 + hours * 3600 - 1e-6:
        clk.t += cycle
        target = start + rate * ((clk.t - t0) / 3600.0)
        moved = False
        while (rate < 0 and reported - target >= 1) or (rate > 0 and target - reported >= 1):
            reported += -1 if rate < 0 else 1
            moved = True
        if moved:
            put(ha, clk, entity, reported)
        ha.calls.clear()
        r = e.evaluate()
        if record is not None:
            record.extend(ha.calls)
        if r["lr"]["reason"] == "window_slope_pause" or r["br"]["reason"] == "window_slope_pause":
            hit = clk.t
            break
    return hit


def bound(rate):
    """lookback + delay + one cycle, measured from the start of the ramp."""
    return LOOKBACK + DELAY + CYCLE


# ---------- pure ----------
def test_slope_defaults_and_clamp():
    assert core.slope_zone_settings({}) == {
        "threshold": 5.4, "delay_s": 120, "max_min": 30, "lookback_min": 20}
    assert core.slope_zone_settings({"window_slope_threshold_f_per_h": 99})["threshold"] == 15
    assert core.slope_zone_settings({"window_slope_threshold_f_per_h": 0})["threshold"] == 2
    assert core.slope_zone_settings({"window_slope_delay_s": "nope"})["delay_s"] == 120
    assert core.slope_zone_settings({"window_slope_max_min": 1})["max_min"] == 5
    assert core.slope_zone_settings({"window_slope_lookback_min": 90})["lookback_min"] == 60
    raw = open(os.path.join(ROOT, "config.yaml"), encoding="utf-8").read()
    assert 'version: "0.9.5"' in raw
    assert "\n  window_slope: true\n" in raw
    assert "window_slope: bool?" in raw
    assert "window_slope_threshold_f_per_h: float(2,15)?" in raw
    assert "window_slope_delay_s: int(0,1800)?" in raw
    assert "window_slope_max_min: int(5,120)?" in raw
    assert "window_slope_lookback_min: int(10,60)?" in raw


def test_slope_math_lookback_not_ema():
    now = 10_000
    # one 1 F step: -3 F/hr over 20 min, under 5.4
    samples = [[now - 7200, 70], [now, 69]]
    assert core.slope_f_per_h(samples, now, 1200) == pytest.approx(-3.0)
    # 1.1 F step is still short of the trigger
    assert core.slope_f_per_h([[now - 7200, 70], [now, 68.9]], now, 1200) == pytest.approx(-3.3)
    # undefined until the history covers the lookback
    assert core.slope_f_per_h([[now - 600, 70], [now, 60]], now, 1200) is None
    # 2 F across the lookback is -6 F/hr
    assert core.slope_f_per_h([[now - 1200, 70], [now, 68]], now, 1200) == pytest.approx(-6.0)


def test_step_rate_limit_and_rearm():
    now = 5_000
    cfg = {"zones": {"lr": core.slope_zone_settings({}), "br": core.slope_zone_settings({})}}
    gates = {"enabled": True, "vacation": False, "defrost": False, "zones": {
        "lr": {"eligible": True, "controlled": True, "person_off": False, "hold": False, "mode": "heat",
               "setpoint": 71, "indoor": 64, "outdoor": 40, "sensor_ok": True},
        "br": {"eligible": True, "controlled": True, "person_off": False, "hold": False, "mode": "heat",
               "setpoint": 71, "indoor": 70, "outdoor": 40, "sensor_ok": True},
    }}
    samples = {"lr": [[now - 7200, 70], [now, 64]], "br": [[now - 7200, 70]]}
    prev = core.empty_slope_state()
    prev["below_since"] = {"lr": now - 120}
    prev["pause_starts"] = {"pair": [now - 100, now - 50]}
    blocked = core.slope_window_step(prev, samples, now, cfg, gates)
    assert blocked["event"] is None and blocked["active"] is False
    prev["pause_starts"] = {"pair": [now - 100]}
    armed = core.slope_window_step(prev, samples, now, cfg, gates)
    assert armed["event"] == "pause" and set(armed["saved"]) == {"lr", "br"}
    # max-duration resume stays disarmed while the slope is still falling
    armed["active"] = True
    armed["max_until"] = now
    armed["trigger_zone"] = "lr"
    armed["saved"] = {"lr": {"mode": "heat", "setpoint": 71, "cmd_mode": "off"}}
    done = core.slope_window_step(armed, samples, now + 1, cfg, gates)
    assert done["event"] == "resume" and done["await_flat"] is True
    again = core.slope_window_step(done, samples, now + 2, cfg, gates)
    assert again["event"] is None and again["active"] is False
    flat = {"lr": [[now - 7200, 64], [now + 3, 64]], "br": [[now - 7200, 70]]}
    clear = core.slope_window_step(again, flat, now + 3, cfg, gates)
    assert clear["await_flat"] is False


def test_step_skips_hold_when_policy_off(monkeypatch):
    monkeypatch.setattr(core, "SLOPE_PAUSE_DURING_HOLD", False)
    now = 8_000
    cfg = {"zones": {"lr": core.slope_zone_settings({}), "br": core.slope_zone_settings({})}}
    gates = {"enabled": True, "vacation": False, "defrost": False, "zones": {
        "lr": {"eligible": True, "controlled": True, "person_off": False, "hold": True, "mode": "heat",
               "setpoint": 74, "indoor": 60, "outdoor": 30, "sensor_ok": True},
        "br": {"eligible": True, "controlled": True, "person_off": False, "hold": False, "mode": "heat",
               "setpoint": 71, "indoor": 70, "outdoor": 30, "sensor_ok": True},
    }}
    samples = {"lr": [[now - 7200, 70], [now, 60]]}
    prev = core.empty_slope_state()
    prev["below_since"] = {"lr": now - 300}
    assert core.slope_window_step(prev, samples, now, cfg, gates)["event"] is None


# ---------- engine ----------
def test_off_matches_094_and_default_on_does_not_move_golden():
    golden = json.load(open(GOLDEN, encoding="utf-8"))
    assert json.loads(defrost_scenario.run(window_slope=False)) == golden
    assert json.loads(defrost_scenario.run()) == golden


@pytest.mark.parametrize("rate", [-6, -10, -20])
def test_quantized_ramps_trigger_inside_the_bound(rate):
    e, ha, clk = world()
    settle(e)
    t0 = clk.t
    hit = drive(e, ha, clk, rate, hours=1)
    assert hit is not None
    assert hit - t0 <= bound(rate)
    r = e.st["last_result"]["zones"]
    assert r["lr"]["reason"] == "window_slope_pause" and r["br"]["reason"] == "window_slope_pause"


def test_three_f_per_hour_never_triggers():
    e, ha, clk = world()
    settle(e)
    assert drive(e, ha, clk, -3, hours=3) is None


def test_single_steps_do_not_pause():
    e, ha, clk = world()
    settle(e)
    put(ha, clk, "sensor.lr_t", 69)  # one 1 F report after a long hold
    for _ in range(15):
        clk.t += CYCLE
        assert e.evaluate()["lr"]["reason"] != "window_slope_pause"
    e, ha, clk = world()
    settle(e)
    put(ha, clk, "sensor.lr_t", 68.9)  # a 1.1 F step is still about 3.3 F/hr
    for _ in range(15):
        clk.t += CYCLE
        assert e.evaluate()["lr"]["reason"] != "window_slope_pause"


def test_unit_flicker_and_stale_fallback_never_feed_the_detector():
    e, ha, clk = world()
    settle(e)
    for i in range(180):
        clk.t += 60
        st, at = ha.s["climate.lr"]
        at = dict(at, current_temperature=70 if i % 2 == 0 else 69)
        ha.s["climate.lr"] = (st, at)
        st2, at2 = ha.s["climate.br"]
        ha.s["climate.br"] = (st2, dict(at2, current_temperature=71 if i % 2 else 70))
        r = e.evaluate()
        assert r["lr"]["reason"] != "window_slope_pause"
    samples = e.st["window_slope"]["samples"]["lr"]
    assert {row[1] for row in samples} == {70}
    # stale room sensor: the unit temperature is the control fallback and must not become the slope
    zones = [dict(z, indoor_stale_min=30) for z in SPLITS]
    e, ha, clk = world(zones=zones)
    ha.reported["sensor.lr_t"] = iso(clk.t - 7200)
    ha.reported["sensor.br_t"] = iso(clk.t - 7200)
    settle(e)
    for i in range(40):
        clk.t += 60
        st, at = ha.s["climate.lr"]
        ha.s["climate.lr"] = (st, dict(at, current_temperature=70 - i))
        assert e.evaluate()["lr"]["reason"] != "window_slope_pause"
    assert "lr" not in (e.st.get("window_slope") or {}).get("samples", {})


def test_setback_mode_outdoor_contact_and_radiant_do_not_pause():
    # sleep setback 71 -> 68 with a slow drift
    e, ha, clk = world()
    settle(e)
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, temperature=68))
    assert drive(e, ha, clk, -1.5, hours=1) is None
    # away setback
    e, ha, clk = world()
    ha.s["device_tracker.phone"] = ("not_home", {})
    settle(e)
    assert drive(e, ha, clk, -2, hours=1) is None
    # mode change blocks a fall that would otherwise pause
    e, ha, clk = world()
    settle(e)
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = ("cool", at)
    clk.t += 60
    e.evaluate()
    ha.s["climate.lr"] = ("heat", at)
    assert drive(e, ha, clk, -20, hours=0.5) is None
    # outdoor not colder
    e, ha, clk = world(outdoor=72)
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5) is None
    # contact sensor zone is the 0.5.0 path, not the slope
    zones = [dict(SPLITS[0], window="binary_sensor.lr_win"), dict(SPLITS[1])]
    e, ha, clk = world(zones=zones)
    ha.s["binary_sensor.lr_win"] = ("off", {})
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5) is None
    # radiant zone
    e, ha, clk = world(zones=[dict(z) for z in SPLITS] + [dict(BATH)])
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5, entity="sensor.bath_t", start=70) is None
    assert e.st["last_result"]["zones"]["bath"]["reason"] != "window_slope_pause"


def test_defrost_and_grace_block_a_real_fall():
    zones = [dict(z, heat_pump=True) for z in SPLITS]
    e, ha, clk = world(zones=zones, outdoor=30, defrost_aware=True)
    settle(e)
    temp = 70.0
    # 0.75 F / 3 min is a defrost-sized dip and also a ~15 F/hr slope. Inference starts
    # within a minute; the slope would trip well before the 15 min cap.
    for _ in range(12):
        clk.t += 60
        temp -= 0.25
        put(ha, clk, "sensor.lr_t", round(temp, 3))
        for eid in ("climate.lr", "climate.br"):
            st, at = ha.s[eid]
            ha.s[eid] = (st, dict(at, hvac_action="heating"))
        assert e.evaluate()["lr"]["reason"] != "window_slope_pause"
    # hold the temperature so the inferred window ends, then fall hard through the grace
    clk.t += 60
    for eid in ("climate.lr", "climate.br"):
        st, at = ha.s[eid]
        ha.s[eid] = (st, dict(at, hvac_action="idle"))
    assert e.evaluate()["lr"]["reason"] != "window_slope_pause"
    for _ in range(8):
        clk.t += 60
        temp -= 1
        put(ha, clk, "sensor.lr_t", round(temp, 3))
        for eid in ("climate.lr", "climate.br"):
            st, at = ha.s[eid]
            ha.s[eid] = (st, dict(at, hvac_action="idle"))
        assert e.evaluate()["lr"]["reason"] != "window_slope_pause"
    # a reported defrost / preheat blocks even when the slope is already steep
    e, ha, clk = world()
    settle(e)
    put(ha, clk, "sensor.lr_t", 64)
    for action in ("defrosting", "preheating"):
        for _ in range(5):
            clk.t += 60
            for eid in ("climate.lr", "climate.br"):
                st, at = ha.s[eid]
                ha.s[eid] = (st, dict(at, hvac_action=action))
            assert e.evaluate()["lr"]["reason"] != "window_slope_pause"


def test_sensor_gap_then_jump_does_not_pause():
    e, ha, clk = world()
    settle(e)
    ha.s["sensor.lr_t"] = ("unavailable", {})
    clk.t += 60
    e.evaluate()
    put(ha, clk, "sensor.lr_t", 50)
    for _ in range(10):
        clk.t += CYCLE
        assert e.evaluate()["lr"]["reason"] != "window_slope_pause"


def test_pause_and_resume_are_paired_and_app_initiated():
    e, ha, clk = world()
    settle(e)
    t0 = clk.t
    hit = drive(e, ha, clk, -20, hours=0.5)
    assert hit is not None and hit - t0 <= bound(-20)
    r = e.st["last_result"]["zones"]
    assert r["lr"]["reason"] == r["br"]["reason"] == "window_slope_pause"
    offs = [c for c in ha.calls if c[0] == "set_hvac_mode"]
    assert {c[1]["entity_id"] for c in offs} >= {"climate.lr", "climate.br"}
    assert {c[1]["hvac_mode"] for c in offs} == {"off"}
    assert "lr" in e.st["app_off"] and "br" in e.st["app_off"]
    temps = dict(e.st.get("override_temps") or {})
    until = dict(e.st["override_until"])
    for _ in range(3):
        clk.t += CYCLE
        ha.calls.clear()
        r = e.evaluate()
        assert r["lr"]["reason"] == "window_slope_pause" and ha.calls == []
        assert (e.st.get("override_temps") or {}) == temps
        assert e.st["override_until"] == until
    for name in ("lr", "br"):
        kinds = [row[1] for row in e.st["events"].get(name, [])]
        assert "window" in kinds and "hold" not in kinds
    # slope back through zero resumes both on the same cycle
    put(ha, clk, "sensor.lr_t", 70)
    clk.t += CYCLE
    ha.calls.clear()
    r = e.evaluate()
    assert r["lr"]["reason"] != "window_slope_pause" and r["br"]["reason"] != "window_slope_pause"
    heats = [c for c in ha.calls if c[0] == "set_hvac_mode"]
    assert {c[1]["entity_id"] for c in heats} == {"climate.lr", "climate.br"}
    assert {c[1]["hvac_mode"] for c in heats} == {"heat"}
    assert e.st["app_off"] == []


def _same_cycle_off(ha, names):
    offs = [c for c in ha.calls if c[0] == "set_hvac_mode" and c[1].get("hvac_mode") == "off"]
    assert {c[1]["entity_id"] for c in offs} >= names


def test_partner_with_contact_sensor_pauses_and_resumes_together():
    zones = [dict(SPLITS[0]), dict(SPLITS[1], window="binary_sensor.br_win")]
    e, ha, clk = world(zones=zones)
    ha.s["binary_sensor.br_win"] = ("off", {})
    settle(e)
    ha.calls.clear()
    assert drive(e, ha, clk, -20, hours=0.5) is not None
    r = e.st["last_result"]["zones"]
    assert r["lr"]["reason"] == r["br"]["reason"] == "window_slope_pause"
    _same_cycle_off(ha, {"climate.lr", "climate.br"})
    put(ha, clk, "sensor.lr_t", 70)
    clk.t += CYCLE
    ha.calls.clear()
    e.evaluate()
    heats = [c for c in ha.calls if c[0] == "set_hvac_mode"]
    assert {c[1]["entity_id"] for c in heats} == {"climate.lr", "climate.br"}
    assert {c[1]["hvac_mode"] for c in heats} == {"heat"}


def test_partner_without_room_sensor_pauses_and_resumes_together():
    br = dict(SPLITS[1])
    br.pop("indoor")
    e, ha, clk = world(zones=[dict(SPLITS[0]), br])
    settle(e)
    ha.calls.clear()
    assert drive(e, ha, clk, -20, hours=0.5) is not None
    r = e.st["last_result"]["zones"]
    assert r["lr"]["reason"] == r["br"]["reason"] == "window_slope_pause"
    _same_cycle_off(ha, {"climate.lr", "climate.br"})
    put(ha, clk, "sensor.lr_t", 70)
    clk.t += CYCLE
    ha.calls.clear()
    e.evaluate()
    heats = [c for c in ha.calls if c[0] == "set_hvac_mode"]
    assert {c[1]["entity_id"] for c in heats} == {"climate.lr", "climate.br"}
    assert {c[1]["hvac_mode"] for c in heats} == {"heat"}


def test_person_off_partner_stays_off():
    e, ha, clk = world()
    ha.s["climate.br"] = ("off", {"temperature": 71, "current_temperature": 70, "hvac_modes": _modes()})
    calls = []
    settle(e)
    calls.extend(ha.calls)
    assert drive(e, ha, clk, -20, hours=0.5, record=calls) is not None
    r = e.st["last_result"]["zones"]
    assert r["lr"]["reason"] == "window_slope_pause"
    assert r["br"]["reason"] == "unit_off"
    assert ha.s["climate.br"][0] == "off"
    assert calls, "the run recorded no service calls to check"
    assert not any(c[1].get("entity_id") == "climate.br" and c[1].get("hvac_mode") == "heat" for c in calls)


def test_manual_change_during_pause_is_kept():
    e, ha, clk = world()
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5) is not None
    assert "lr" in e.st["window_slope"]["saved"] and "br" in e.st["window_slope"]["saved"]
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = ("heat", dict(at, temperature=75))
    clk.t += CYCLE
    r = e.evaluate()
    assert "lr" not in e.st["window_slope"]["saved"]
    assert r["lr"]["reason"] == "manual_override"
    assert e.st["override_temps"]["lr"] == 75
    assert "br" in e.st["window_slope"]["saved"]
    put(ha, clk, "sensor.lr_t", 70)
    clk.t += CYCLE
    e.evaluate()
    assert ha.s["climate.lr"][0] == "heat"
    assert ha.s["climate.lr"][1]["temperature"] == 75


def test_hold_pauses_with_partner_and_keeps_memory(monkeypatch):
    e, ha, clk = world()
    settle(e)
    e.start_override("lr")
    e.remember_override_temp(e.zone("lr"), 74)
    until = dict(e.st["override_until"])
    temps = dict(e.st["override_temps"])
    assert drive(e, ha, clk, -20, hours=0.5) is not None
    assert e.st["last_result"]["zones"]["lr"]["reason"] == "window_slope_pause"
    assert e.st["last_result"]["zones"]["br"]["reason"] == "window_slope_pause"
    assert e.st["override_until"] == until and e.st["override_temps"] == temps
    put(ha, clk, "sensor.lr_t", 74)
    clk.t += CYCLE
    e.evaluate()
    assert e.st["override_until"] == until and e.st["override_temps"] == temps
    assert e._override_active("lr", clk.t)
    # the other policy: a hold skips the pair pause
    monkeypatch.setattr(core, "SLOPE_PAUSE_DURING_HOLD", False)
    e, ha, clk = world()
    settle(e)
    e.start_override("lr")
    e.remember_override_temp(e.zone("lr"), 74)
    assert drive(e, ha, clk, -20, hours=0.5) is None


def test_restart_mid_pause_and_max_duration(tmp_path):
    path = str(tmp_path / "state.json")
    e, ha, clk = world(path=path)
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5) is not None
    e.save()
    e2 = Engine(e.opts, ha, path, clock=clk, localtime=lt14)
    assert set(e2.st["app_off"]) == {"lr", "br"}
    assert e2.st["window_slope"]["active"] is True
    r = e2.evaluate()
    assert r["lr"]["reason"] == "window_slope_pause" and r["br"]["reason"] == "window_slope_pause"
    # past the max, the next cycle resumes even while the slope is still negative
    e2.st["window_slope"]["max_until"] = clk.t - 1
    clk.t += CYCLE
    r = e2.evaluate()
    assert r["lr"]["reason"] != "window_slope_pause"
    assert ha.s["climate.lr"][0] == "heat" and ha.s["climate.br"][0] == "heat"
    # no re-pause while the fall continues
    put(ha, clk, "sensor.lr_t", 60)
    clk.t += CYCLE
    assert e2.evaluate()["lr"]["reason"] != "window_slope_pause"


def test_pair_pause_cap_survives_restart(tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="smart_hvac")
    path = str(tmp_path / "state.json")
    e, ha, clk = world(path=path)
    settle(e)
    t = clk.t
    e.st["window_slope"]["pause_starts"] = {"pair": [t - 600, t - 300]}
    e.save()
    caplog.clear()
    e2 = Engine(e.opts, ha, path, clock=clk, localtime=lt14)
    assert e2.st["window_slope"]["pause_starts"]["pair"] == [t - 600, t - 300]
    assert "pruned state of removed zones: pair" not in caplog.text
    assert drive(e2, ha, clk, -20, hours=0.5) is None


def test_malformed_slope_state_resets(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"window_slope": "nope"}))
    e = Engine(opts(), HA({}), str(p), clock=Clock(), localtime=lt14)
    e.evaluate()
    assert e.st["window_slope"]["active"] is False


def test_api_block_while_paused():
    e, ha, clk = world()
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5) is not None
    for z in e.snapshot()["zones"]:
        block = z["window_slope"]
        assert set(block) == {"active", "since", "max_until", "slope_f_per_h", "trigger_zone"}
        assert block["active"] is True
        assert block["trigger_zone"] == "lr"
        assert isinstance(block["since"], (int, float)) and isinstance(block["max_until"], (int, float))
        assert block["slope_f_per_h"] is None or isinstance(block["slope_f_per_h"], float)


def test_no_split_modes_and_no_lone_pause():
    rng = random.Random(5)
    e, ha, clk = world()
    settle(e)
    for _ in range(36):
        clk.t += 90
        for ent in ("sensor.lr_t", "sensor.br_t"):
            cur = float(ha.s[ent][0])
            nxt = cur + rng.choice([0, 0, -1, -1, -4, 1])
            put(ha, clk, ent, nxt)
        outdoor = rng.choice([20, 35, 70])
        ha.s["weather.w"] = ("cloudy", {"temperature": outdoor, "cloud_coverage": 100})
        if rng.random() < 0.2:
            ha.s["climate.br"] = ("off", ha.s["climate.br"][1])
        ha.calls.clear()
        r = e.evaluate()
        fams = [core.mode_family(d["hvac_mode"]) for svc, d in ha.calls
                if svc == "set_hvac_mode" and d["entity_id"] in ("climate.lr", "climate.br")]
        assert "heat" not in fams or "cool" not in fams
        wants = [r[n]["mode"] for n in ("lr", "br") if r[n]["action"] == "set"]
        assert "heat" not in wants or "cool" not in wants
        paused = [n for n in ("lr", "br") if r[n]["reason"] == "window_slope_pause"]
        if len(paused) == 1:
            other = "br" if paused[0] == "lr" else "lr"
            assert r[other]["reason"] == "unit_off"
        heating = [n for n in ("lr", "br") if r[n].get("mode") == "heat" and r[n]["action"] == "set"]
        assert not (paused and heating)


def test_new_reason_text_and_accent_have_no_amber():
    files = [
        os.path.join(ROOT, "rootfs", "app", "static", "index.html"),
        os.path.join(ROOT, "rootfs", "app", "static", "thermostat.html"),
        os.path.join(ROOT, "rootfs", "app", "static", "classic.html"),
        os.path.join(ROOT, "lovelace", "smart-hvac-card.js"),
    ]
    banned = ("orange", "amber", "#d9902b", "#e08a3c", "#f0c27a", "#e8a05a", "#e2c07a")
    for path in files:
        text = open(path, encoding="utf-8").read()
        assert "Paused, window open?" in text
        start, end = text.find("/* slope-pause-start */"), text.find("/* slope-pause-end */")
        assert start >= 0 and end > start
        chunk = text[start:end].lower()
        assert "#3e6ae1" in chunk
        for word in banned:
            assert word not in chunk


def test_option_off_does_not_pause():
    e, ha, clk = world(window_slope=False)
    settle(e)
    assert drive(e, ha, clk, -20, hours=0.5) is None
    assert "window_slope" not in e.st
