"""0.4.1 season guard: in heating weather a room above the sleep target coasts in heat, never cools."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import core  # noqa: E402
from engine import Engine  # noqa: E402
from test_040 import Clock, lt  # noqa: E402
from test_engine import FakeHA, opts, states  # noqa: E402

SPLIT = {"facing_az": 225, "solar_factor": 0.8, "heat_only": False}


def inp(indoor, outdoor, minute=22 * 60 + 35, fc=None, hum=62, last_mode="heat", last_sp=71.0):
    return {"enabled": True, "manual_profile": "auto", "bryson_home": True, "minute_of_day": minute,
            "now_ts": 1_800_000_000, "outdoor_temp": outdoor, "forecast_temp": fc, "cloud_pct": 0,
            "sun_elev": -10, "sun_az": 0, "indoor_temp": indoor, "humidity": hum, "window_open": None,
            "unit_available": True, "last_mode": last_mode, "last_setpoint": last_sp,
            "last_mode_change_ts": 0, "app_turned_off": False}


@pytest.mark.parametrize("indoor", [70.0, 70.5, 71.0, 71.5])
@pytest.mark.parametrize("outdoor,fc", [(58, None), (45, 52), (62, 66), (36, None)])
def test_live_case_october_sleep_never_cools(indoor, outdoor, fc):
    r = core.decide(inp(indoor, outdoor, fc=fc), SPLIT, {"home": 71, "guest": 71})
    assert r["profile"] == "sleep" and r["mode"] == "heat" and r["setpoint"] == 68.0  # coast, no humid offset


def test_default_lockout_is_75():
    assert core.DEFAULTS["cool_lockout"] == 75.0


def test_hot_weather_still_cools():
    r = core.decide(inp(80, 88), SPLIT, {})
    assert r["mode"] == "cool"


def test_forecast_high_counts():
    # mild now but a hot forecast high -> normal deadband logic may cool
    assert core.pick_mode(72, 68, 70, "heat", dict(core.DEFAULTS), outdoor_hi=80) == "cool"
    assert core.pick_mode(72, 68, 70, "heat", dict(core.DEFAULTS), outdoor_hi=74) == "heat"


def test_option_lowers_lockout_and_unit_already_cooling_returns_to_heat():
    c = dict(core.DEFAULTS, cool_lockout=60)
    assert core.pick_mode(71, 68, 58, "heat", c) == "heat"  # 58 < 60
    assert core.pick_mode(71, 68, 65, "heat", c) == "cool"  # 65 >= 60: old deadband behaviour
    assert core.pick_mode(71, 68, 58, "cool", dict(core.DEFAULTS)) == "heat"


def test_engine_option_and_live_projection_tonight():
    """Live 30 Oct 8:52 AM: 71 indoor, 58 out, home 71 -> next was 'cool 67.5 at 10:32 PM'. Now heat 68."""
    s = states(**{"weather.w": ("sunny", {"temperature": 58, "cloud_coverage": 0}),
                  "sensor.lr_t": ("71", {}), "sensor.lr_h": ("62", {}),
                  "climate.lr": ("heat", {"temperature": 71, "current_temperature": 71})})
    e = Engine(opts(), FakeHA(s), None, clock=Clock(), localtime=lt(8, 52))
    n = e.evaluate()["lr"]["next"]
    assert n["mode"] == "heat" and n["setpoint"] == 68.0 and n["profile"] == "sleep"
    e2 = Engine(opts(cool_lockout=55), FakeHA(s), None, clock=Clock(), localtime=lt(8, 52))
    assert e2._cfg(e2.zone("lr"))["cool_lockout"] == 55.0


def test_projection_moves_room_toward_setpoint(monkeypatch):
    import engine
    seen = []
    real = engine.core.decide

    def spy(i, u, c=None):
        seen.append(i["indoor_temp"])
        return real(i, u, c)
    monkeypatch.setattr(engine.core, "decide", spy)
    n = Engine.next_change(inp(74.0, 58, minute=14 * 60), SPLIT, {"home": 71, "guest": 71},
                           {"reason": "no_change", "mode": "heat"})
    assert seen[0] == 74.0 and seen[1] < 74.0 and abs(seen[-1] - 71.0) < 0.3  # 74 -> ~71 by 22:30
    assert n["setpoint"] == 68.0 and n["mode"] == "heat"
