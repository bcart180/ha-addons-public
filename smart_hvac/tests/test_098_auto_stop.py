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


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fuzz_outages_and_opposite_choices_never_opposite_never_raise(seed):
    rng = random.Random(seed)
    e, ha, clk = world(auto_stop_sensitivity="fast", min_cycle_min=5, override_hours=1)
    saved_attr = {x: ha.s[x][1] for x in SPLITS}
    for _ in range(400):
        for ent in ("sensor.lr_t", "sensor.br_t"):
            room(ha, ent, "unavailable" if rng.random() < 0.05 else round(rng.uniform(68.5, 73.5), 1))
        ha.s["weather.w"] = ("cloudy", {"temperature": rng.choice([30, 40, 80, 95]), "cloud_coverage": 100})
        for ent in SPLITS:
            roll = rng.random()
            if roll < 0.03:
                ha.s[ent] = ("unavailable", {})
            elif roll < 0.10 and ha.s[ent][0] == "unavailable":
                ha.s[ent] = ("off", saved_attr[ent])
            elif roll < 0.13 and ha.s[ent][0] != "unavailable":
                ha.s[ent] = (rng.choice(["off", "heat", "cool"]), ha.s[ent][1])
        r, calls = step(e, ha, clk)  # never raises
        fams = {core.mode_family(ha.s[x][0]) for x in SPLITS} - {None}
        assert len(fams) <= 1, (fams, calls)
        wants = {core.mode_family(r[n]["mode"]) for n in ("lr", "br") if r[n]["action"] == "set"} - {None}
        assert len(wants) <= 1


# ---------- MA review fixes (M-1, M-2, L-1, L-2, L-3) ----------
def test_m1_unavailable_room_sensor_never_stops_on_the_head_reading():
    e, ha, clk = world()
    room(ha, "sensor.lr_t", "unavailable")  # the head still reports 71.5 at its own height
    r, calls = step(e, ha, clk, 16)
    assert mode_writes(calls) == []
    assert e.st["auto_stop"]["active"] is False
    assert "lr" not in e.st["auto_stop"]["samples"]


def test_m1_stale_room_sensor_never_stops():
    e, ha, clk = world(zones=zones(lr={"indoor_stale_min": 30}))
    ha.reported["sensor.lr_t"] = iso(clk.t - 4 * 3600)  # frozen reading
    r, calls = step(e, ha, clk, 16)
    assert mode_writes(calls) == []
    assert e.st["auto_stop"]["active"] is False


def test_m1_room_sensor_lost_while_stopped_restarts_both():
    e, ha, clk = world()
    n, _r, _c = run_until_stop(e, ha, clk)
    assert n is not None
    stopped_at = clk.t
    room(ha, "sensor.br_t", "unavailable")
    r, calls = step(e, ha, clk, 4)
    assert sorted(mode_writes(calls)) == [("climate.br", "heat"), ("climate.lr", "heat")]
    assert e.st["auto_stop"]["restart_reason"] == "needed"
    assert e.st["auto_stop"]["last_restart"] - stopped_at >= 15 * 60


def test_m2_opposite_wanted_releases_without_forcing_the_saved_mode():
    e, ha, clk = world()
    n, _r, _c = run_until_stop(e, ha, clk)
    assert n is not None
    # the house is now hot and it is hot outside: the schedule wants cool, never a heat run first
    ha.s["weather.w"] = ("sunny", {"temperature": 95, "cloud_coverage": 0})
    for ent in ("sensor.lr_t", "sensor.br_t"):
        room(ha, ent, 77)
    calls, seen = [], []
    for _ in range(12):
        r, c = step(e, ha, clk)
        calls += c
        seen.append({n: r[n]["reason"] for n in ("lr", "br")})
        fams = {core.mode_family(ha.s[x][0]) for x in SPLITS} - {None}
        assert len(fams) <= 1
    assert e.st["auto_stop"]["restart_reason"] == "opposite"
    assert ("climate.lr", "heat") not in mode_writes(calls) and ("climate.br", "heat") not in mode_writes(calls)
    assert all(v != "auto_stop_restart" for d in seen for v in d.values())
    assert ha.s["climate.lr"][0] == ha.s["climate.br"][0] == "cool"


def test_m2_core_reason_opposite_is_a_release():
    st = core.empty_auto_stop_state()
    st.update(active=True, mode="heat", since=0, saved={"lr": {"mode": "heat", "setpoint": 71}})
    gates = {"enabled": True, "blocked": None,
             "zones": {"lr": {"member": True, "controlled": True, "mode": "off", "indoor": 77, "target": 76,
                              "opposite": True, "slope": 0, "changed_ts": 0}}}
    new = core.auto_stop_step(st, 3600, {"window_s": 1800, "min_cycle_s": 900}, gates)
    assert new["event"] == "restart" and new["reason"] == "opposite"
    assert "opposite" in core.AUTO_STOP_RELEASE_REASONS and "needed" not in core.AUTO_STOP_RELEASE_REASONS


def test_l1_unavailable_head_blocks_stop():
    e, ha, clk = world()
    ha.s["climate.br"] = ("unavailable", {})
    r, calls = step(e, ha, clk, 16)
    assert mode_writes(calls) == []
    assert e.st["auto_stop"]["active"] is False
    assert e.snapshot()["auto_stop"]["blocked"] == "unavailable"


def test_l1_core_unknown_member_mode_blocks():
    st = core.empty_auto_stop_state()
    z = {"member": True, "controlled": True, "mode": "heat", "indoor": 72, "target": 71, "slope": 0,
         "changed_ts": 0}
    gates = {"enabled": True, "blocked": None, "zones": {"lr": z, "br": dict(z, mode=None)}}
    new = core.auto_stop_step(st, 10_000, {"window_s": 1800, "min_cycle_s": 900}, gates)
    assert new["event"] is None and new["reason"] == "unavailable" and new["all_since"] is None


def test_l2_l4_person_sets_opposite_on_a_stopped_head_others_wait_off():
    e, ha, clk = world()
    run_until_stop(e, ha, clk)
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = ("cool", at)  # a person picks the other direction at the remote
    calls, parked_seen = [], False
    for _ in range(10):
        r, c = step(e, ha, clk)
        calls += c
        fams = {core.mode_family(ha.s[x][0]) for x in SPLITS} - {None}
        assert len(fams) <= 1
        if ha.s["climate.lr"][0] == "cool":  # MA L-4: while the person's head cools, the other waits off
            assert ha.s["climate.br"][0] == "off" and r["br"]["reason"] == "auto_stop_wait"
            assert e.snapshot()["auto_stop"]["parked"] == ["br"]
            assert "br" in e.st["app_off"] and "br" not in e.st["override_until"]
            parked_seen = True
    assert parked_seen
    assert e.st["auto_stop"]["restart_reason"] == "manual_opposite"
    assert e.st["override_until"].get("lr")
    # the other head is never driven to the person's direction, so there is no group-wide reversal
    assert ("climate.br", "cool") not in mode_writes(calls)


def test_l4_park_ends_with_the_hold_and_survives_a_restart(tmp_path):
    p = str(tmp_path / "state.json")
    e, ha, clk = world(path=p, override_hours=1, min_cycle_min=120)  # the person's cool lasts the whole hold
    run_until_stop(e, ha, clk)
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = ("cool", at)
    step(e, ha, clk, 2)
    e2 = Engine(e.opts, ha, p, clock=clk, localtime=lt14)  # add-on restart while parked
    assert e2.st["auto_stop"]["park"]["heads"] == ["br"] and "br" in e2.st["app_off"]
    released = None
    for i in range(20):
        step(e2, ha, clk)
        fams = {core.mode_family(ha.s[x][0]) for x in SPLITS} - {None}
        assert len(fams) <= 1
        if not e2.st["auto_stop"].get("park"):
            released = i
            break
        assert ha.s["climate.lr"][0] == "cool" and ha.s["climate.br"][0] == "off"
    assert released is not None
    assert not e2._override_active("lr", clk.t)  # released exactly because the hold ended


def test_l4_park_releases_when_the_person_returns_to_the_saved_direction():
    e, ha, clk = world()
    run_until_stop(e, ha, clk)
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = ("cool", at)
    step(e, ha, clk, 2)
    assert e.st["auto_stop"]["park"]
    ha.s["climate.lr"] = ("heat", ha.s["climate.lr"][1])  # back to the saved direction
    step(e, ha, clk)
    assert not e.st["auto_stop"].get("park")


def test_l4_park_record_is_sanitized():
    ok = core.copy_auto_stop({"park": {"by": ["lr"], "heads": ["br", "lr", 3], "mode": "heat"}})
    assert ok["park"] == {"by": ["lr"], "heads": ["br"], "mode": "heat"}
    for bad in ({"by": [], "heads": ["br"], "mode": "heat"}, {"by": ["lr"], "heads": ["br"], "mode": "off"},
                "x", {"by": "lr", "heads": ["br"], "mode": "heat"}):
        assert core.copy_auto_stop({"park": bad})["park"] is None
    assert core.copy_auto_stop({"active": True, "mode": "heat", "saved": {"br": {"mode": "heat"}},
                                "park": {"by": ["lr"], "heads": ["br"], "mode": "heat"}})["park"] is None


def test_i4_missing_room_sensor_shows_a_blocked_reason():
    e, ha, clk = world()
    room(ha, "sensor.lr_t", "unavailable")
    step(e, ha, clk, 2)
    assert e.snapshot()["auto_stop"]["blocked"] == "sensor"


def test_l3_unknown_contact_sensor_blocks_stop():
    z = zones(lr={"window": "binary_sensor.lr_w"})
    e, ha, clk = world(zones=z)
    ha.s["binary_sensor.lr_w"] = ("unavailable", {})
    r, calls = step(e, ha, clk, 16)
    assert mode_writes(calls) == []
    assert e.snapshot()["auto_stop"]["blocked"] == "window"
    ha.s["binary_sensor.lr_w"] = ("off", {})  # a closed, readable window lets the window run
    n, _r, calls = run_until_stop(e, ha, clk)
    assert n is not None and n * CYCLE >= 1800


@pytest.mark.parametrize("rel,guard", [
    ("rootfs/app/static/thermostat.html", 'const astop=off&&r.reason==="auto_stop";'),
    ("rootfs/app/static/classic.html", 'r.reason==="auto_stop"&&(r.live||{}).mode==="off"?"Idle"'),
    ("rootfs/app/static/index.html", 'r.reason === "auto_stop" && live.mode === "off" ? "Idle"'),
])
def test_i1_panels_read_idle_only_once_the_head_is_off(rel, guard):
    assert guard in open(os.path.join(ROOT, rel), encoding="utf-8").read()


def test_m3_head_unavailable_during_stop_never_breaks_the_cycle(tmp_path):
    p = str(tmp_path / "state.json")
    e, ha, clk = world(path=p)
    n, _r, _c = run_until_stop(e, ha, clk)
    assert n is not None
    ha.s["climate.br"] = ("unavailable", {})  # the head drops off while no room needs heat
    bst, bat = ha.s["climate.bath"]
    ha.s["climate.bath"] = (bst, dict(bat, temperature=65))  # the radiant floor needs a new setpoint
    os.remove(p)
    bath_sp = []
    for _ in range(6):
        clk.t += CYCLE
        ha.calls.clear()
        r = e.evaluate()  # used to raise KeyError every cycle
        assert r["lr"]["reason"] == "auto_stop"
        assert all(d.get("entity_id") != "climate.br" for _svc, d in ha.calls)  # nothing forced at it
        # the radiant floor still gets its own decision and writes on the same cycle
        bath_sp += [d for svc, d in ha.calls if svc == "set_temperature" and d["entity_id"] == "climate.bath"]
    assert bath_sp, "setpoint writes for the other zones continue"
    assert json.load(open(p))["auto_stop"]["active"] is True  # state keeps being saved
    # a room needing heat still restarts the head that is there
    room(ha, "sensor.lr_t", 69.0)
    _r, calls = step(e, ha, clk, 2)
    assert ("climate.lr", "heat") in mode_writes(calls)


def test_m3_write_modes_safely_tolerates_an_unreadable_head():
    e, ha, clk = world()
    step(e, ha, clk)
    ha.s["climate.br"] = ("unavailable", {})
    zs = {z["name"]: z for z in e.opts["zones"]}
    states = {s["entity_id"]: s for s in ha.get_states()}
    pending = [("br", zs["br"], {"action": "set", "mode": "off"}, None, None, None, None, None)]
    ok = e._write_modes_safely(pending, states)  # used to raise KeyError
    assert ok["br"] is False  # not known to be off, so not reported as applied


# ---------- options, docs, UI ----------
def test_options_schema_and_translations():
    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    assert cfg["version"] == "0.9.11"
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
