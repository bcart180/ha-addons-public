"""0.9.8 whole-house auto-stop. Generic fixtures only (climate.lr / climate.br, sensor.*_t)."""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402
import yaml  # noqa: E402

import core  # noqa: E402
import defrost_scenario  # noqa: E402
from engine import Engine  # noqa: E402
from test_040 import Clock  # noqa: E402
from test_095_slope_window import HA, iso  # noqa: E402
from test_engine import NOW, lt14  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "defrost_off_golden.json")
CYCLE = 300
SPLITS = ("climate.lr", "climate.br")


def zones(**extra):
    out = [
        {"name": "lr", "label": "Room A", "climate": "climate.lr", "indoor": "sensor.lr_t",
         "facing_az": 180, "solar_factor": 0, "heat_only": False, "enabled": True},
        {"name": "br", "label": "Room B", "climate": "climate.br", "indoor": "sensor.br_t",
         "facing_az": 90, "solar_factor": 0, "heat_only": False, "enabled": True},
        {"name": "bath", "label": "Bath", "climate": "climate.bath", "indoor": "sensor.bath_t",
         "facing_az": 90, "solar_factor": 0, "heat_only": True, "enabled": True, "comfort": 72},
    ]
    for z in out:
        z.update(extra.get(z["name"], {}))
    return out


def opts(**kw):
    o = {"enabled": True, "dry_run": False, "window_slope": False, "presence_entity": "device_tracker.phone",
         "weather_entity": "weather.w", "min_cycle_min": 15, "override_hours": 2,
         "auto_stop_enabled": True, "auto_stop_sensitivity": "slow",
         "profiles": {"home": 71, "guest": 71, "sleep": 68, "away_heat": 65, "away_cool": 78},
         "zones": kw.pop("zones", None) or zones()}
    o.update(kw)
    return o


def world(lr=71.5, br=71.5, outdoor=40, mode="heat", path=None, **kw):
    modes = ["off", "heat", "cool", "fan_only"]
    s = {
        "device_tracker.phone": ("home", {}),
        "weather.w": ("cloudy", {"temperature": outdoor, "cloud_coverage": 100}),
        "sun.sun": ("below_horizon", {"elevation": -10, "azimuth": 0}),
        "climate.lr": (mode, {"temperature": 71, "current_temperature": lr, "hvac_modes": modes,
                              "hvac_action": "idle"}),
        "sensor.lr_t": (str(lr), {}),
        "climate.br": (mode, {"temperature": 71, "current_temperature": br, "hvac_modes": modes,
                              "hvac_action": "idle"}),
        "sensor.br_t": (str(br), {}),
        "climate.bath": ("heat", {"temperature": 72, "current_temperature": 70, "hvac_modes": ["heat", "off"]}),
        "sensor.bath_t": ("70", {}),
    }
    ha, clk = HA(s), Clock()
    clk.t = NOW
    e = Engine(opts(**kw), ha, path, clock=clk, localtime=lt14)
    return e, ha, clk


def room(ha, entity, value):
    ha.s[entity] = (str(value), {})


def step(e, ha, clk, n=1):
    """Run n cycles. Returns (results of the last cycle, every service call made)."""
    calls, r = [], None
    for _ in range(n):
        clk.t += CYCLE
        ha.calls.clear()
        r = e.evaluate()
        calls.extend(ha.calls)
    return r, calls


def mode_writes(calls):
    return [(d["entity_id"], d["hvac_mode"]) for svc, d in calls
            if svc == "set_hvac_mode" and d["entity_id"] in SPLITS]


def run_until_stop(e, ha, clk, limit=40):
    for i in range(1, limit + 1):
        r, calls = step(e, ha, clk)
        if r["lr"]["reason"] == "auto_stop" or r["br"]["reason"] == "auto_stop":
            return i, r, calls
    return None, r, []


# ---------- pure core ----------
def test_levels_and_default():
    assert core.AUTO_STOP_LEVELS == {"very_slow": 60, "slow": 30, "medium": 15, "fast": 7}
    assert core.auto_stop_window_min("slow") == 30
    assert core.auto_stop_window_min(None) == 30
    assert core.auto_stop_window_min("bogus") == 30
    assert core.auto_stop_window_min("fast") == 7


def test_satisfied_and_needed_math():
    w = 1800
    assert core.auto_stop_satisfied("heat", 71.0, 71.0, 0.0, w)
    assert core.auto_stop_satisfied("heat", 71.5, 71.0, -0.5, w)  # projects 71.25: still at target
    assert not core.auto_stop_satisfied("heat", 71.2, 71.0, -1.0, w)  # projects 70.7: below target
    assert not core.auto_stop_satisfied("heat", 70.9, 71.0, 2.0, w)
    assert core.auto_stop_satisfied("cool", 70.5, 71.0, 0.5, w)
    assert not core.auto_stop_satisfied("cool", 71.5, 71.0, 0.0, w)
    assert not core.auto_stop_satisfied("off", 71.5, 71.0, 0.0, w)
    assert not core.auto_stop_satisfied("heat", None, 71.0, 0.0, w)
    assert core.auto_stop_needed("heat", 70.4, 71.0, 0.0, w)
    assert not core.auto_stop_needed("heat", 70.6, 71.0, 0.0, w)
    assert core.auto_stop_needed("heat", 71.0, 71.0, -1.2, w)  # projects 70.4
    assert core.auto_stop_needed("cool", 71.6, 71.0, None, w)
    assert core.auto_stop_needed("heat", None, 71.0, 0.0, w)  # missing room: fail safe, restart


def _gates(**zone_kw):
    base = {"member": True, "controlled": True, "mode": "heat", "indoor": 71.5, "target": 71.0, "slope": 0.0,
            "person_off": False, "changed_ts": None}
    zs = {"a": dict(base), "b": dict(base)}
    for name, kw in zone_kw.items():
        zs[name].update(kw)
    return {"enabled": True, "blocked": None, "zones": zs}


CFG = {"window_s": 1800, "min_cycle_s": 900}


def test_step_needs_whole_window_and_all_zones():
    st = core.auto_stop_step(None, 0, CFG, _gates())
    assert st["event"] is None and st["all_since"] == 0
    st = core.auto_stop_step(st, 1799, CFG, _gates())
    assert st["event"] is None
    st = core.auto_stop_step(st, 1800, CFG, _gates())
    assert st["event"] == "stop" and st["mode"] == "heat" and set(st["saved"]) == {"a", "b"}
    one_short = core.auto_stop_step(None, 0, CFG, _gates(b={"indoor": 70.8}))
    assert one_short["all_since"] is None and one_short["event"] is None


def test_step_never_mixed_or_uncontrolled():
    assert core.auto_stop_step({"all_since": -9999}, 0, CFG, _gates(b={"mode": "cool"}))["event"] is None
    assert core.auto_stop_step({"all_since": -9999}, 0, CFG, _gates(b={"mode": "auto"}))["event"] is None
    unc = core.auto_stop_step({"all_since": -9999}, 0, CFG, _gates(b={"controlled": False}))
    assert unc["event"] is None and unc["reason"] == "uncontrolled"
    for why in ("defrost", "window", "hold"):
        g = dict(_gates(), blocked=why)
        blocked = core.auto_stop_step({"all_since": -9999}, 0, CFG, g)
        assert blocked["event"] is None and blocked["all_since"] is None and blocked["reason"] == why


def test_step_restart_waits_min_cycle_then_restarts_all():
    st = core.auto_stop_step({"all_since": -1800}, 0, CFG, _gates())
    assert st["event"] == "stop"
    cold = _gates(a={"indoor": 70.0})
    held = core.auto_stop_step(st, 899, CFG, cold)
    assert held["event"] is None and held["active"] is True
    go = core.auto_stop_step(held, 900, CFG, cold)
    assert go["event"] == "restart" and go["reason"] == "needed" and set(go["saved"]) == {"a", "b"}
    manual = core.auto_stop_step(st, 10, CFG, _gates(a={"diverged": True}))
    assert manual["event"] == "restart" and manual["reason"] == "manual"
    off = core.auto_stop_step(st, 10, CFG, dict(_gates(), enabled=False))
    assert off["event"] == "restart" and off["reason"] == "option_off"


def test_malformed_state_is_reset():
    for bad in ("x", 3, {"active": True, "saved": "nope"}, {"active": True, "mode": "auto", "saved": {}}):
        st = core.copy_auto_stop(bad)
        assert st["active"] is False and st["saved"] == {}


# ---------- engine ----------
def test_flag_off_matches_097():
    golden = json.load(open(GOLDEN, encoding="utf-8"))
    assert json.loads(defrost_scenario.run(auto_stop_enabled=False)) == golden
    assert json.loads(defrost_scenario.run()) == golden
    e, ha, clk = world(auto_stop_enabled=False)
    r, calls = step(e, ha, clk, 36)
    assert mode_writes(calls) == []
    assert all(r[n]["reason"] != "auto_stop" for n in ("lr", "br"))
    assert "auto_stop" not in e.st
    assert e.snapshot()["auto_stop"]["enabled"] is False and e.snapshot()["auto_stop"]["active"] is False


def test_all_satisfied_stops_after_window_together():
    e, ha, clk = world()
    t0 = clk.t
    n, r, calls = run_until_stop(e, ha, clk)
    assert n is not None
    elapsed = clk.t - t0
    assert 1800 <= elapsed <= 1800 + 2 * CYCLE
    assert sorted(mode_writes(calls)) == [("climate.br", "off"), ("climate.lr", "off")]
    assert r["lr"]["reason"] == r["br"]["reason"] == "auto_stop"
    assert r["bath"]["reason"] != "auto_stop"  # radiant is not on the outdoor unit
    assert ha.s["climate.bath"][0] == "heat"
    snap = e.snapshot()["auto_stop"]
    assert snap["active"] is True and snap["mode"] == "heat" and snap["zones"] == ["br", "lr"]
    assert snap["window_min"] == 30 and snap["sensitivity"] == "slow"


def test_no_stop_before_the_window():
    e, ha, clk = world()
    r, calls = step(e, ha, clk, 5)  # 25 min
    assert mode_writes(calls) == []
    assert all(r[n]["reason"] != "auto_stop" for n in ("lr", "br"))


def test_one_zone_unsatisfied_never_stops():
    e, ha, clk = world(lr=71.5, br=70.5)
    r, calls = step(e, ha, clk, 36)
    assert mode_writes(calls) == []
    assert e.st["auto_stop"]["active"] is False


def test_falling_trend_does_not_stop():
    e, ha, clk = world(lr=72.0, br=71.5)
    calls = []
    for i in range(12):
        room(ha, "sensor.lr_t", 72.0 - 0.1 * i)  # -1.2 F/h: projects below 71 within 30 min
        _r, c = step(e, ha, clk)
        calls += c
    assert ("climate.lr", "off") not in mode_writes(calls)


def test_defrost_blocks_stop():
    e, ha, clk = world(defrost_aware=True)
    st, at = ha.s["climate.lr"]
    calls, blocked = [], set()
    for i in range(24):  # 10 min defrost every 30 min for 2 h: the 30 min window never completes
        act = "defrosting" if i % 6 in (0, 1) else "idle"
        ha.s["climate.lr"] = (st, dict(at, hvac_action=act))
        _r, c = step(e, ha, clk)
        calls += c
        blocked.add(e.snapshot()["auto_stop"]["blocked"])
    assert mode_writes(calls) == []
    assert "defrost" in blocked
    # the window starts from zero once defrost and its grace are over
    ha.s["climate.lr"] = (st, dict(at, hvac_action="idle"))
    n, _r, _c = run_until_stop(e, ha, clk)
    assert n is not None and n * CYCLE >= 1800


def test_open_window_blocks_stop():
    z = zones(br={"window": "binary_sensor.br_win", "window_action": "off"})
    e, ha, clk = world(zones=z)
    ha.s["binary_sensor.br_win"] = ("on", {})
    r, calls = step(e, ha, clk, 12)
    assert ("climate.lr", "off") not in mode_writes(calls)
    assert e.snapshot()["auto_stop"]["blocked"] == "window"


def test_slope_window_pause_blocks_stop():
    e, ha, clk = world(window_slope=True)
    step(e, ha, clk, 3)
    saved = {n: {"mode": "heat", "setpoint": 71, "ts": clk.t, "cmd_mode": "off", "cmd_setpoint": None}
             for n in ("lr", "br")}
    e.st["window_slope"] = dict(core.empty_slope_state(), active=True, since=clk.t, trigger_zone="lr",
                                max_until=clk.t + 7200, saved=saved)
    r, calls = step(e, ha, clk, 9)
    assert all(r[n]["reason"] == "window_slope_pause" for n in ("lr", "br"))
    assert e.snapshot()["auto_stop"]["blocked"] == "window"
    assert e.st["auto_stop"]["active"] is False


def test_manual_hold_blocks_stop():
    e, ha, clk = world()
    e.start_override("lr", 4)
    r, calls = step(e, ha, clk, 12)
    assert mode_writes(calls) == []
    assert e.snapshot()["auto_stop"]["blocked"] == "hold"


def test_min_cycle_respected_before_stop():
    e, ha, clk = world(auto_stop_sensitivity="fast", min_cycle_min=30)
    ha.changed["climate.lr"] = iso(clk.t)  # mode just changed
    t0 = clk.t
    n, r, calls = run_until_stop(e, ha, clk)
    assert n is not None and clk.t - t0 >= 1800
    assert sorted(mode_writes(calls)) == [("climate.br", "off"), ("climate.lr", "off")]


def test_min_cycle_respected_before_restart_and_restart_is_together_in_saved_mode():
    e, ha, clk = world(min_cycle_min=20)
    n, _r, _c = run_until_stop(e, ha, clk)
    assert n is not None
    stopped_at = clk.t
    room(ha, "sensor.br_t", 69.5)  # br is now cold
    restarted = None
    for _ in range(10):
        r, calls = step(e, ha, clk)
        writes = mode_writes(calls)
        if writes:
            restarted = (clk.t, writes, r)
            break
        assert ha.s["climate.lr"][0] == "off" and ha.s["climate.br"][0] == "off"
    assert restarted is not None
    t, writes, r = restarted
    assert t - stopped_at >= 20 * 60
    assert sorted(writes) == [("climate.br", "heat"), ("climate.lr", "heat")]
    assert r["lr"]["reason"] == r["br"]["reason"] == "auto_stop_restart"
    sp = [d for svc, d in calls if svc == "set_temperature" and d["entity_id"] in SPLITS]
    assert {d["entity_id"] for d in sp} <= set(SPLITS)


def test_restart_in_cool_when_stopped_in_cool():
    e, ha, clk = world(lr=70.5, br=70.5, outdoor=90, mode="cool")
    n, _r, calls = run_until_stop(e, ha, clk)
    assert n is not None and sorted(mode_writes(calls)) == [("climate.br", "off"), ("climate.lr", "off")]
    assert e.st["auto_stop"]["mode"] == "cool"
    room(ha, "sensor.lr_t", 72.0)
    _r, calls = step(e, ha, clk, 4)
    assert sorted(mode_writes(calls)) == [("climate.br", "cool"), ("climate.lr", "cool")]


def test_stop_and_restart_are_not_manual_takeovers():
    e, ha, clk = world()
    run_until_stop(e, ha, clk)
    r, _c = step(e, ha, clk, 2)
    assert e.st["override_until"] == {}
    assert all(r[n]["reason"] == "auto_stop" and r[n]["action"] == "hold" for n in ("lr", "br"))
    assert set(e.st["app_off"]) >= {"lr", "br"}
    room(ha, "sensor.lr_t", 69.0)
    _r, calls = step(e, ha, clk, 4)
    assert ("climate.lr", "heat") in mode_writes(calls)
    r, _c = step(e, ha, clk, 2)
    assert e.st["override_until"] == {}
    assert all(r[n]["reason"] != "manual_override" for n in ("lr", "br"))
    assert "lr" not in e.st["app_off"] and "br" not in e.st["app_off"]


def test_person_turning_a_head_on_restarts_the_others_same_mode():
    e, ha, clk = world()
    run_until_stop(e, ha, clk)
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = ("heat", at)  # a person turns one head back on at the remote
    r, calls = step(e, ha, clk)
    assert ("climate.br", "heat") in mode_writes(calls)
    assert e.st["override_until"].get("lr")  # the person's change is a hold, as before
    assert e.st["auto_stop"]["active"] is False and e.st["auto_stop"]["restart_reason"] == "manual"
    assert ha.s["climate.lr"][0] == ha.s["climate.br"][0] == "heat"


def test_option_off_while_stopped_restarts_both():
    e, ha, clk = world()
    run_until_stop(e, ha, clk)
    e.opts["auto_stop_enabled"] = False
    r, calls = step(e, ha, clk)
    assert sorted(mode_writes(calls)) == [("climate.br", "heat"), ("climate.lr", "heat")]
    r, calls = step(e, ha, clk, 3)
    assert "auto_stop" not in e.st


def test_stop_survives_addon_restart(tmp_path):
    p = str(tmp_path / "state.json")
    e, ha, clk = world(path=p, min_cycle_min=5)
    run_until_stop(e, ha, clk)
    e2 = Engine(e.opts, ha, p, clock=clk, localtime=lt14)
    assert set(e2.st["app_off"]) >= {"lr", "br"}
    assert e2.st["auto_stop"]["active"] is True
    room(ha, "sensor.lr_t", 69.0)
    clk.t += 600
    ha.calls.clear()
    e2.evaluate()
    assert sorted(mode_writes(ha.calls)) == [("climate.br", "heat"), ("climate.lr", "heat")]


def test_dry_run_writes_nothing():
    e, ha, clk = world(dry_run=True)
    n, r, calls = run_until_stop(e, ha, clk)
    assert n is not None
    assert all(svc != "set_hvac_mode" for svc, _d in calls)
    assert r["lr"]["reason"] == "auto_stop" and r["lr"]["applied"] is False
    assert ha.s["climate.lr"][0] == "heat"


def test_disabled_split_blocks_stop():
    z = zones(br={"enabled": False})
    e, ha, clk = world(zones=z)
    r, calls = step(e, ha, clk, 12)
    assert mode_writes(calls) == []
    assert e.snapshot()["auto_stop"]["blocked"] == "uncontrolled"


def test_fuzz_never_opposite_modes():
    rng = random.Random(98)
    e, ha, clk = world(auto_stop_sensitivity="fast", min_cycle_min=5)
    for _ in range(300):
        for ent in ("sensor.lr_t", "sensor.br_t"):
            room(ha, ent, round(rng.uniform(68.5, 73.5), 1))
        ha.s["weather.w"] = ("cloudy", {"temperature": rng.choice([30, 40, 80, 95]), "cloud_coverage": 100})
        if rng.random() < 0.05:
            ent = rng.choice(SPLITS)
            ha.s[ent] = (rng.choice(["off", "heat", "cool"]), ha.s[ent][1])
        r, calls = step(e, ha, clk)
        fams = {core.mode_family(ha.s[x][0]) for x in SPLITS} - {None}
        assert len(fams) <= 1, (fams, calls)
        wants = {core.mode_family(r[n]["mode"]) for n in ("lr", "br") if r[n]["action"] == "set"} - {None}
        assert len(wants) <= 1


# ---------- options, docs, UI ----------
def test_options_schema_and_translations():
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    assert cfg["version"] == "0.9.8"
    assert cfg["options"]["auto_stop_enabled"] is False
    assert cfg["options"]["auto_stop_sensitivity"] == "slow"
    assert cfg["schema"]["auto_stop_enabled"] == "bool?"
    assert cfg["schema"]["auto_stop_sensitivity"] == "list(very_slow|slow|medium|fast)?"
    tr = yaml.safe_load(open(os.path.join(ROOT, "translations", "en.yaml"), encoding="utf-8"))
    assert "auto_stop_enabled" in tr["configuration"] and "auto_stop_sensitivity" in tr["configuration"]
    docs = open(os.path.join(ROOT, "DOCS.md"), encoding="utf-8").read()
    assert "auto_stop_enabled" in docs and "auto_stop_sensitivity" in docs
    assert "## 0.9.8" in open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()


@pytest.mark.parametrize("rel", [
    ("rootfs", "app", "static", "index.html"),
    ("rootfs", "app", "static", "thermostat.html"),
    ("rootfs", "app", "static", "classic.html"),
    ("lovelace", "smart-hvac-card.js"),
])
def test_reason_text_uses_the_blue_accent(rel):
    text = open(os.path.join(ROOT, *rel), encoding="utf-8").read()
    assert "Idle, all rooms at target" in text
    assert "auto_stop" in text
    start, end = text.find("/* slope-pause-start */"), text.find("/* slope-pause-end */")
    chunk = text[start:end].lower()
    assert "#3e6ae1" in chunk
    for word in ("orange", "amber", "#d9902b", "#e08a3c", "#f0c27a", "#e8a05a", "#e2c07a"):
        assert word not in chunk
