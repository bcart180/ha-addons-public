"""0.5.1 defrost_aware (MA design review 30 Oct: bounded windows, fail-safe inference, off = 0.5.0)."""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import defrost_scenario  # noqa: E402
from engine import (DEFROST_AWARE_CAP_S, DEFROST_AWARE_MAX_PER_HOUR, DEFROST_INFER_OUTDOOR_MAX_F,  # noqa: E402
                    Engine)
from test_040 import Clock, act  # noqa: E402
from test_engine import ZONES, FakeHA, lt14, opts, states  # noqa: E402


def golden():
    with open(defrost_scenario.GOLDEN) as f:
        return f.read()


# ---------- MA 3: off = byte-identical to 0.5.0 ----------
def test_option_absent_is_byte_identical_to_050():
    assert defrost_scenario.run() == golden()


def test_option_false_is_byte_identical_to_050():
    assert defrost_scenario.run(defrost_aware=False) == golden()


def test_option_on_differs_from_golden():
    assert defrost_scenario.run(defrost_aware=True) != golden()


def test_bounds_constants():
    assert DEFROST_AWARE_CAP_S == 15 * 60 and DEFROST_AWARE_MAX_PER_HOUR == 4
    assert DEFROST_INFER_OUTDOOR_MAX_F == 40.0


def setup(heat_pump=True, outdoor=30, **o):
    zones = [dict(z, heat_pump=heat_pump) if z["name"] == "lr" else dict(z) for z in ZONES]
    s = states()
    s["weather.w"] = ("cloudy", {"temperature": outdoor, "cloud_coverage": 100}) if outdoor is not None else (
        "unavailable", {})
    ha, clk = FakeHA(s), Clock()
    e = Engine(opts(zones=zones, defrost_aware=True, **o), ha, None, clock=clk, localtime=lt14)
    e.evaluate()  # app writes 71
    return e, ha, clk


def room(ha, t, a="heating"):
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, hvac_action=a, current_temperature=t))
    ha.s["sensor.lr_t"] = (str(t), {})


def step(e, clk, dt=60):
    clk.t += dt
    return e.evaluate()


# ---------- MA 2: official always honoured ----------
@pytest.mark.parametrize("hp,outdoor", [(False, None), (False, 60), (True, None)])
def test_official_defrosting_always_honoured(hp, outdoor):
    e, ha, clk = setup(heat_pump=hp, outdoor=outdoor)
    room(ha, 66, "defrosting")
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75))
    r = step(e, clk)
    assert r["lr"]["defrost"] == "defrosting" and r["lr"]["defrost_inferred"] is False
    # unknown outdoor: core's fail-safe hold reason wins; still no write and no manual-change hold
    assert r["lr"]["reason"] == ("defrosting" if outdoor is not None else "missing:outdoor_temp")
    assert not r["lr"]["applied"]
    assert e.snapshot()["zones"][0]["override_until"] is None


# ---------- MA 1: window cap ----------
def test_window_capped_at_15_min_then_normal_logic_and_flag():
    e, ha, clk = setup()
    room(ha, 66, "defrosting")
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75))  # person's change mid-defrost
    for _ in range(3):  # 0, 5, 10, 15 min after first seen: exempt
        r = step(e, clk, 300)
        assert r["lr"]["reason"] == "defrosting"
    r = step(e, clk, 300)
    assert r["lr"]["reason"] == "defrosting"  # exactly 15 min: still inside
    r = step(e, clk, 300)  # 20 min: over the cap
    assert r["lr"]["defrost"] is None and r["lr"]["defrost_stuck"] and r["lr"]["reason"] != "defrosting"
    assert e.snapshot()["zones"][0]["override_until"]  # the 75 now counts as a manual change


# ---------- MA 1: episodes per hour ----------
def test_fifth_window_in_an_hour_is_not_exempt():
    e, ha, clk = setup()
    for i in range(DEFROST_AWARE_MAX_PER_HOUR):
        room(ha, 66, "defrosting")
        assert step(e, clk, 300)["lr"]["reason"] == "defrosting", i
        room(ha, 66, "heating")
        assert step(e, clk, 300)["lr"]["defrost"] is None
    room(ha, 66, "defrosting")
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75))
    r = step(e, clk, 300)  # 5th window within 60 min
    assert r["lr"]["defrost"] is None and r["lr"]["reason"] != "defrosting" and not r["lr"]["defrost_stuck"]
    assert e.snapshot()["zones"][0]["override_until"]


def test_episode_budget_frees_after_an_hour():
    e, ha, clk = setup()
    for _ in range(DEFROST_AWARE_MAX_PER_HOUR):
        room(ha, 66, "defrosting")
        step(e, clk, 300)
        room(ha, 66, "heating")
        step(e, clk, 300)
    clk.t += 3600
    room(ha, 66, "defrosting")
    assert step(e, clk, 300)["lr"]["reason"] == "defrosting"


# ---------- inference ----------
def test_inferred_on_fast_drop_while_heating_cold_outside():
    e, ha, clk = setup()
    room(ha, 66.0)
    step(e, clk, 180)
    room(ha, 65.2)  # 0.8 F in 3 min while "heating"
    r = step(e, clk, 180)
    assert r["lr"]["defrost"] == "defrosting" and r["lr"]["defrost_inferred"] is True


@pytest.mark.parametrize("outdoor", [None, "unknown"])
def test_unknown_outdoor_temp_means_no_inference(outdoor):
    e, ha, clk = setup()
    if outdoor == "unknown":
        ha.s["weather.w"] = ("cloudy", {"temperature": "unknown", "cloud_coverage": 100})
    else:
        ha.s["weather.w"] = ("unavailable", {})
    room(ha, 66.0)
    step(e, clk, 180)
    room(ha, 64.0)
    r = step(e, clk, 180)
    assert r["lr"]["defrost"] is None and r["lr"]["defrost_inferred"] is False


def test_no_inference_when_warm_outside_or_not_heat_pump_or_not_heating():
    for kw, a, mode in (({"outdoor": DEFROST_INFER_OUTDOOR_MAX_F + 1}, "heating", "heat"),
                        ({"heat_pump": False}, "heating", "heat"),
                        ({}, "idle", "heat"), ({}, "heating", "cool")):
        e, ha, clk = setup(**kw)
        room(ha, 66.0, a)
        step(e, clk, 180)
        room(ha, 64.0, a)
        ha.s["climate.lr"] = (mode, ha.s["climate.lr"][1])
        r = step(e, clk, 180)
        assert r["lr"]["defrost"] is None and not r["lr"]["defrost_inferred"], (kw, a, mode)


def test_slow_drop_is_not_inferred():
    e, ha, clk = setup()
    room(ha, 66.0)
    step(e, clk, 300)
    room(ha, 65.5)  # 0.3 F per 3 min
    assert step(e, clk, 300)["lr"]["defrost"] is None


# ---------- MA 4: no new writes / entities, debug logs only ----------
def test_defrost_aware_adds_no_ha_calls_beyond_engine_writes_and_logs_debug_only(caplog):
    e, ha, clk = setup()
    n = len(ha.calls)
    with caplog.at_level(logging.DEBUG, logger="smart_hvac"):
        room(ha, 66.0)
        step(e, clk, 180)
        room(ha, 65.0)
        step(e, clk, 180)
        ha.s["weather.w"] = ("unavailable", {})
        room(ha, 64.0)
        step(e, clk, 180)
    assert ha.calls[n:] == []
    mine = [r for r in caplog.records if r.getMessage().startswith("defrost ")]
    assert mine and all(r.levelno == logging.DEBUG for r in mine)


def test_removed_zone_defrost_aware_state_pruned(tmp_path):
    import json
    p = str(tmp_path / "st.json")
    json.dump({"defrost_aware": {"gone": {"samples": []}}}, open(p, "w"))
    e = Engine(opts(defrost_aware=True), FakeHA(states()), p, localtime=lt14)
    assert e.st["defrost_aware"] == {}


def test_act_helper_still_used():
    e, ha, clk = setup()
    act(ha, "climate.lr", "preheating")
    assert step(e, clk)["lr"]["reason"] == "preheating"
