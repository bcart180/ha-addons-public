"""0.5.0 window delay + restore (contact sensor only, no slope detection)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import core  # noqa: E402
from engine import Engine  # noqa: E402
from test_040 import Clock  # noqa: E402
from test_engine import ZONES, FakeHA, lt14, opts, states  # noqa: E402


def zones(**kw):
    z = [dict(x) for x in ZONES]
    z[0].update(window="binary_sensor.lr_win", **kw)
    return z


def setup(win="off", **kw):
    s = states(**{"binary_sensor.lr_win": (win, {})})
    s["climate.lr"] = ("heat", {"temperature": 65, "current_temperature": 66, "hvac_modes": ["heat", "cool", "off"]})
    ha, clk = FakeHA(s), Clock()
    e = Engine(opts(zones=zones(**kw)), ha, None, clock=clk, localtime=lt14)
    e.evaluate()  # settle at 71 heat
    ha.calls.clear()
    return e, ha, clk


def win(ha, v):
    ha.s["binary_sensor.lr_win"] = (v, {})


# ---------- core.window_debounce ----------
def test_debounce_waits_for_open_delay():
    st = core.window_debounce(None, True, 0, 120, 0)
    assert not st["confirmed"] and st["event"] is None
    st = core.window_debounce(st, True, 119, 120, 0)
    assert not st["confirmed"]
    st = core.window_debounce(st, True, 120, 120, 0)
    assert st["confirmed"] and st["event"] == "opened"
    st = core.window_debounce(st, True, 500, 120, 0)
    assert st["confirmed"] and st["event"] is None


def test_debounce_quick_open_close_never_triggers():
    st = core.window_debounce(None, True, 0, 300, 0)
    st = core.window_debounce(st, False, 60, 300, 0)
    st = core.window_debounce(st, False, 600, 300, 0)
    assert not st["confirmed"] and st["event"] is None


def test_debounce_close_delay_and_unreadable_keeps_state():
    st = core.window_debounce(None, True, 0, 0, 600)
    assert st["event"] == "opened"
    assert core.window_debounce(st, None, 10, 0, 600)["confirmed"]
    st = core.window_debounce(st, False, 100, 0, 600)
    assert st["confirmed"]
    st = core.window_debounce(st, False, 700, 0, 600)
    assert not st["confirmed"] and st["event"] == "closed"


def test_zero_delay_is_immediate():
    assert core.window_debounce(None, True, 5)["event"] == "opened"


# ---------- core.window_target ----------
@pytest.mark.parametrize("action,modes,exp", [
    ("off", ["heat", "off"], ("off", 71)),
    ("fan_only", ["heat", "fan_only", "off"], ("fan_only", 71)),
    ("fan_only", ["heat", "off"], ("off", 71)),  # unit without fan_only -> off
    ("eco", ["heat", "off"], ("heat", 65.0)),
    ("bogus", ["heat"], ("off", 71)),
])
def test_window_target(action, modes, exp):
    assert core.window_target(action, "heat", 71, modes, {}) == exp


def test_window_target_eco_cool_uses_away_cool():
    assert core.window_target("eco", "cool", 72, ["cool"], {}) == ("cool", 78.0)


def test_actions_list_matches_config_schema():
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    assert "window_action: list(%s)?" % "|".join(core.WINDOW_ACTIONS) in raw
    assert "window_open_delay_s: int(0,3600)?" in raw and "window_close_delay_s: int(0,3600)?" in raw


# ---------- engine ----------
def test_default_action_off_is_inert_like_pre_050():
    """MA L1 / Info: window_action off (default) saves nothing and restores nothing: open -> off (as 0.4.1),
    close -> the normal schedule resumes through decide (reason ok), exactly as before 0.5.0."""
    e, ha, clk = setup()
    win(ha, "on")
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "window_open" and r["lr"]["mode"] == "off"
    assert e.st["window_saved"] == {}
    ha.calls.clear()
    clk.t += 300
    assert e.evaluate()["lr"]["reason"] == "window_open" and ha.calls == []
    win(ha, "off")
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "ok" and r["lr"]["mode"] == "heat" and r["lr"]["setpoint"] == 71.0


def test_open_delay_ignores_short_opening():
    e, ha, clk = setup(window_open_delay_s=600)
    win(ha, "on")
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] != "window_open" and ha.calls == []
    win(ha, "off")
    clk.t += 300
    e.evaluate()
    assert ha.calls == [] and "lr" not in e.st["window_saved"]


def test_eco_action_keeps_mode_sets_setback():
    e, ha, clk = setup(window_action="eco")
    win(ha, "on")
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["mode"] == "heat" and r["lr"]["setpoint"] == 65.0
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 65.0}) in ha.calls
    clk.t += 300
    e.evaluate()  # app's own setback is not seen as a manual change
    assert not e._override_active("lr", clk.t)
    win(ha, "off")
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "window_restore" and ha.s["climate.lr"][1]["temperature"] == 71.0


def test_close_delay_holds_off_until_elapsed():
    e, ha, clk = setup(window_close_delay_s=600, window_action="eco")
    win(ha, "on")
    clk.t += 300
    e.evaluate()
    win(ha, "off")
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "window_open"
    clk.t += 299  # 599 s closed
    assert e.evaluate()["lr"]["reason"] == "window_open"
    clk.t += 301  # 900 s closed
    assert e.evaluate()["lr"]["reason"] == "window_restore"


def test_no_restore_when_person_turned_unit_off_during_window():
    e, ha, clk = setup(window_action="eco")
    win(ha, "on")
    clk.t += 300
    e.evaluate()
    ha.s["climate.lr"] = ("off", ha.s["climate.lr"][1])  # person's off at the wall
    clk.t += 300
    e.evaluate()
    win(ha, "off")
    clk.t += 300
    ha.calls.clear()
    r = e.evaluate()
    assert r["lr"]["reason"] == "unit_off" and ha.calls == []


def test_dry_run_never_writes():
    e, ha, clk = setup()
    e.opts["dry_run"] = True
    win(ha, "on")
    clk.t += 300
    e.evaluate()
    win(ha, "off")
    clk.t += 300
    r = e.evaluate()
    assert ha.calls == [] and not r["lr"]["applied"]


def test_zone_without_window_sensor_unchanged():
    ha, clk = FakeHA(states()), Clock()
    e = Engine(opts(), ha, None, clock=clk, localtime=lt14)
    e.evaluate()
    assert e.st["window"] == {} and e.st["window_saved"] == {}


def test_removed_zone_window_state_pruned():
    e, ha, clk = setup()
    e.st["window"]["gone"] = {"confirmed": True}
    e.st["window_saved"]["gone"] = {"mode": "heat"}
    assert "gone" in e.prune_zones()


# ---------- CoS: discard saved state on vacation / manual change after the window opened ----------
def open_window(action="eco"):
    e, ha, clk = setup(window_action=action)
    win(ha, "on")
    clk.t += 300
    e.evaluate()
    assert "lr" in e.st["window_saved"]
    return e, ha, clk


def close_and_eval(e, ha, clk):
    win(ha, "off")
    clk.t += 300
    ha.calls.clear()
    return e.evaluate()


def test_vacation_after_open_discards_saved_state():
    e, ha, clk = open_window()
    ha.s["input_boolean.vacation_mode"] = ("on", {})
    clk.t += 300
    e.evaluate()
    assert "lr" not in e.st["window_saved"]
    r = close_and_eval(e, ha, clk)
    assert r["lr"]["reason"] != "window_restore"
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 71.0}) not in ha.calls


def test_manual_mode_change_after_open_discards_saved_state():
    e, ha, clk = open_window()
    ha.s["climate.lr"] = ("cool", ha.s["climate.lr"][1])  # someone switches the mode
    clk.t += 300
    e.evaluate()
    assert "lr" not in e.st["window_saved"]
    r = close_and_eval(e, ha, clk)
    assert r["lr"]["reason"] != "window_restore" and ha.calls == []


def test_manual_setpoint_change_after_open_discards_saved_state():
    e, ha, clk = open_window()
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=69))
    clk.t += 300
    e.evaluate()
    assert "lr" not in e.st["window_saved"]
    r = close_and_eval(e, ha, clk)
    assert r["lr"]["reason"] != "window_restore" and ha.calls == []


def test_person_off_after_open_discards_saved_state():
    e, ha, clk = open_window()
    ha.s["climate.lr"] = ("off", ha.s["climate.lr"][1])
    clk.t += 300
    e.evaluate()
    assert "lr" not in e.st["window_saved"]


# ---------- MA L1: restore goes through core.decide (gates + clamps) ----------
def closed_with_saved(e, ha, clk, name, saved):
    """Window confirmed open with a saved state, then closed: the next evaluate() is the restore cycle."""
    e.st["window"][name] = {"confirmed": True, "raw": True, "since": clk.t}
    e.st["window_saved"][name] = dict(saved, ts=clk.t)
    win(ha, "off")
    clk.t += 300
    ha.calls.clear()
    return e.evaluate()


def test_restore_cool_below_cool_lockout_is_blocked():
    e, ha, clk = setup(window_action="eco")  # outdoor 45 < cool_lockout 75
    r = closed_with_saved(e, ha, clk, "lr", {"mode": "cool", "setpoint": 72.0})
    assert r["lr"]["mode"] == "heat" and r["lr"]["reason"] != "window_restore"
    assert not any(c[1].get("hvac_mode") == "cool" for c in ha.calls)
    assert "lr" not in e.st["window_saved"]


def test_restore_uses_saved_setpoint_when_mode_matches():
    e, ha, clk = setup(window_action="eco")
    r = closed_with_saved(e, ha, clk, "lr", {"mode": "heat", "setpoint": 69.0})
    assert r["lr"]["reason"] == "window_restore" and r["lr"]["setpoint"] == 69.0


def test_restore_clamped_to_min_max():
    e, ha, clk = setup(window_action="eco")
    r = closed_with_saved(e, ha, clk, "lr", {"mode": "heat", "setpoint": 99.0})
    assert r["lr"]["setpoint"] == 84.0  # core max_temp


def test_radiant_restore_above_radiant_setpoint_max_is_clamped():
    z = [dict(x) for x in ZONES]
    z[1].update(window="binary_sensor.lr_win", window_action="eco")
    s = states(**{"binary_sensor.lr_win": ("off", {})})
    ha, clk = FakeHA(s), Clock()
    e = Engine(opts(zones=z, radiant_setpoint_max=78), ha, None, clock=clk, localtime=lt14)
    e.evaluate()
    r = closed_with_saved(e, ha, clk, "bath", {"mode": "heat", "setpoint": 90.0})
    assert r["bath"]["reason"] == "window_restore" and r["bath"]["setpoint"] == 78.0
    assert ("set_temperature", {"entity_id": "climate.bath", "temperature": 78.0}) in ha.calls


def test_minisplit_restore_during_vacation_uses_vacation_target():
    e, ha, clk = setup(window_action="eco")
    ha.s["input_boolean.vacation_mode"] = ("on", {})
    r = closed_with_saved(e, ha, clk, "lr", {"mode": "heat", "setpoint": 71.0})
    assert r["lr"]["reason"] != "window_restore" and r["lr"]["setpoint"] == 60.0
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 71.0}) not in ha.calls


# ---------- MA L3: window must be a binary_sensor ----------
def test_window_schema_is_binary_sensor_match():
    import re
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    line = r"window: match(^binary_sensor\.[a-z0-9_]+$)?"
    assert line in raw
    rx = re.compile(r"^binary_sensor\.[a-z0-9_]+$")
    assert rx.match("binary_sensor.lr_window") and not rx.match("sensor.lr_window")
    assert not rx.match("input_boolean.x") and not rx.match("binary_sensor.Bad-Id")


# ---------- MA L4: window sensor removed from options -> saved state dropped ----------
def test_removed_window_sensor_drops_saved_state():
    e, ha, clk = setup(window_action="eco")
    win(ha, "on")
    clk.t += 300
    e.evaluate()
    assert "lr" in e.st["window_saved"] and "lr" in e.st["window"]
    e.opts["zones"][0].pop("window")
    clk.t += 300
    e.evaluate()
    assert "lr" not in e.st["window_saved"] and "lr" not in e.st["window"]
