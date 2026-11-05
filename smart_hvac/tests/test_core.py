import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
from core import decide, solar_gain, pick_profile, radiant_overtemp, DEFAULTS

LR = {"facing_az": 225, "solar_factor": 0.8}
RAD = {"facing_az": 90, "solar_factor": 0, "heat_only": True}
NOW = 1_800_000_000


def base(**kw):
    d = dict(enabled=True, override_active=False, manual_profile="auto", owner_home=True,
             minute_of_day=14 * 60, now_ts=NOW, outdoor_temp=50, forecast_temp=None, cloud_pct=100,
             sun_elev=-10, sun_az=0, indoor_temp=66, humidity=45, window_open=None,
             unit_available=True, last_mode="heat", last_setpoint=65, last_mode_change_ts=NOW - 7200)
    d.update(kw)
    return d


def test_disabled_holds():
    assert decide(base(enabled=False), LR)["action"] == "hold"

def test_home_cold_heats_to_71():
    r = decide(base(), LR)
    assert (r["action"], r["mode"], r["setpoint"], r["profile"]) == ("set", "heat", 71.0, "home")

def test_home_hot_cools():
    r = decide(base(indoor_temp=76, outdoor_temp=85, last_mode="cool", last_setpoint=78), LR)
    assert r["mode"] == "cool" and r["setpoint"] == 71.0

def test_away_heat_setback():
    r = decide(base(owner_home=False), LR)
    assert r["profile"] == "away" and r["mode"] == "heat" and r["setpoint"] == 65.0

def test_away_cool_setback():
    r = decide(base(owner_home=False, indoor_temp=80, outdoor_temp=90, last_mode="cool"), LR)
    assert r["mode"] == "cool" and r["setpoint"] == 78.0

def test_guest_profile_overrides_absent_owner():
    r = decide(base(owner_home=False, manual_profile="guest"), LR)
    assert r["profile"] == "guest" and r["setpoint"] == 71.0

def test_sleep_window():
    r = decide(base(minute_of_day=23 * 60), LR)
    assert r["profile"] == "sleep" and r["setpoint"] == 68.0

def test_preheat_before_wake():
    r = decide(base(minute_of_day=6 * 60 + 30), LR)
    assert r["profile"] == "home" and r["setpoint"] == 71.0

def test_sleep_early_morning_not_preheat():
    assert pick_profile("auto", True, 3 * 60, DEFAULTS) == "sleep"

def test_solar_gain_lowers_heat_target():
    r = decide(base(sun_elev=45, sun_az=225, cloud_pct=0), LR)
    assert r["setpoint"] < 71.0 and r["setpoint"] >= 69.5

def test_clouds_cancel_solar():
    r = decide(base(sun_elev=45, sun_az=225, cloud_pct=100), LR)
    assert r["setpoint"] == 71.0

def test_solar_zero_at_night_and_wrong_facing():
    assert solar_gain(-5, 225, 0, 225, 1) == 0.0
    assert solar_gain(45, 45, 0, 225, 1) == 0.0

def test_humid_cool_offset():
    r = decide(base(indoor_temp=76, outdoor_temp=85, humidity=70, last_mode="cool", last_setpoint=78), LR)
    assert r["setpoint"] == 70.5

def test_window_open_turns_off():
    r = decide(base(window_open=True), LR)
    assert r["mode"] == "off" and r["reason"] == "window_open"

def test_missing_indoor_holds_last():
    r = decide(base(indoor_temp="unavailable"), LR)
    assert r["action"] == "hold" and r["setpoint"] == 65 and r["reason"] == "missing:indoor_temp"

def test_missing_outdoor_holds():
    assert decide(base(outdoor_temp=None), LR)["reason"] == "missing:outdoor_temp"

def test_missing_presence_holds():
    assert decide(base(owner_home=None), LR)["reason"] == "missing:presence"

def test_unit_unavailable_holds():
    assert decide(base(unit_available=False), LR)["reason"] == "missing:unit"

def test_short_cycle_guard():
    r = decide(base(indoor_temp=76, outdoor_temp=85, last_mode="heat", last_mode_change_ts=NOW - 300), LR)
    assert r["action"] == "hold" and r["reason"] == "short_cycle_guard"

def test_short_cycle_released_after_min():
    r = decide(base(indoor_temp=76, outdoor_temp=85, last_mode="heat", last_mode_change_ts=NOW - 1000), LR)
    assert r["mode"] == "cool"

def test_defrosting_skips_short_cycle_and_setpoint_chase():
    blocked = decide(base(indoor_temp=76, outdoor_temp=85, last_mode="heat", last_mode_change_ts=NOW - 300), LR)
    assert blocked["reason"] == "short_cycle_guard"
    freed = decide(base(indoor_temp=76, outdoor_temp=85, last_mode="heat",
                        last_mode_change_ts=NOW - 300, defrosting=True), LR)
    assert freed["reason"] != "short_cycle_guard" and freed["action"] == "set" and freed["mode"] == "cool"
    chase = decide(base(defrosting=True), LR)  # heat already, setpoint 65 vs target 71
    assert chase["action"] == "hold" and chase["reason"] == "defrosting"

def test_override_holds():
    assert decide(base(override_active=True), LR)["reason"] == "manual_override"

def test_no_change_holds():
    r = decide(base(last_setpoint=71.0), LR)
    assert r["reason"] == "no_change"

def test_deadband_outdoor_tiebreak_and_forecast():
    r = decide(base(indoor_temp=71, outdoor_temp=60, forecast_temp=80, last_mode="heat", last_mode_change_ts=0), LR)
    assert r["mode"] == "cool"  # (60+80)/2=70 >= 68

def test_radiant_heat_only():
    r = decide(base(indoor_temp=80, outdoor_temp=90, last_mode="heat"), RAD)
    assert r["mode"] == "heat"

def test_garbage_input_fails_safe():
    r = decide(base(minute_of_day="x"), LR)
    assert r["action"] == "hold" and r["reason"].startswith("error")

RAD_CFG = {"home": 72.0, "guest": 72.0, "sleep": 66.0, "away_heat": 60.0, "away_cool": 60.0}

def test_radiant_comfort_home():
    r = decide(base(indoor_temp=68), RAD, RAD_CFG)
    assert (r["mode"], r["setpoint"]) == ("heat", 72.0)

def test_radiant_away_setback():
    r = decide(base(owner_home=False, indoor_temp=68), RAD, RAD_CFG)
    assert (r["mode"], r["setpoint"], r["profile"]) == ("heat", 60.0, "away")

def test_radiant_sleep_night():
    r = decide(base(minute_of_day=0, indoor_temp=68), RAD, RAD_CFG)
    assert r["profile"] == "sleep" and r["setpoint"] == 66.0

def test_radiant_missing_presence_holds():
    assert decide(base(owner_home=None), RAD, RAD_CFG)["action"] == "hold"

# MA M1: never turn a unit back on from an off the app did not command
def test_physical_off_is_respected():
    r = decide(base(last_mode="off", indoor_temp=60), LR)
    assert (r["action"], r["reason"], r["mode"]) == ("hold", "unit_off", "off")

def test_radiant_wall_off_is_respected():
    r = decide(base(last_mode="off", indoor_temp=60), RAD, RAD_CFG)
    assert r["action"] == "hold" and r["reason"] == "unit_off"

def test_app_commanded_off_can_resume():
    r = decide(base(last_mode="off", app_turned_off=True, indoor_temp=60), LR)
    assert (r["action"], r["mode"]) == ("set", "heat")

# MA L1: radiant over-temp cap
def test_radiant_overtemp_room_cap():
    assert radiant_overtemp(80.5) is True  # default air shutoff 80
    assert radiant_overtemp(79.9) is False

def test_radiant_overtemp_floor_cap_and_missing():
    assert radiant_overtemp(70, 86) is True
    assert radiant_overtemp(70, 84) is False
    assert radiant_overtemp(None, None) is False
    assert radiant_overtemp("unavailable", "unknown") is False
