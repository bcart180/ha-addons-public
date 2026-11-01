"""0.4.0: defrost awareness, presets, next change / why, event strip, legacy cap log."""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

from engine import DEFROST_ACTIONS, VACATION_ENTITY, Engine  # noqa: E402
from server import PRESETS, make_app  # noqa: E402
from test_engine import NOW, FakeHA, lt14, opts, states  # noqa: E402


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def lt(h, m=0):
    return lambda _t=None: time.struct_time((2026, 10, 30, h, m, 0, 4, 303, 0))


def setup(local=lt14, **kw):
    ha, clk = FakeHA(states(**kw)), Clock()
    return Engine(opts(), ha, None, clock=clk, localtime=local), ha, clk


def act(ha, eid, a):
    st, at = ha.s[eid]
    ha.s[eid] = (st, dict(at, hvac_action=a))


# ---------- A) defrost ----------
def test_defrost_values_are_has_standard_ones():
    assert DEFROST_ACTIONS == ("defrosting", "preheating")


@pytest.mark.parametrize("a", ["defrosting", "preheating"])
def test_defrost_change_is_not_manual_hold_and_no_write(a):
    e, ha, clk = setup()
    e.evaluate()  # app writes 71
    clk.t += 300
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75, hvac_action=a))
    ha.calls.clear()
    r = e.evaluate()
    assert r["lr"]["reason"] == a and r["lr"]["action"] == "hold" and r["lr"]["defrost"] == a
    assert [c for c in ha.calls if c[1]["entity_id"] == "climate.lr"] == []
    assert e.snapshot()["zones"][0]["override_until"] is None
    assert r["lr"]["next"] is None


def test_defrost_does_not_count_as_mode_change_for_short_cycle():
    e, ha, clk = setup()
    e.evaluate()
    t0 = e.st["last_change"].get("lr")
    clk.t += 300
    ha.s["climate.lr"] = ("cool", dict(ha.s["climate.lr"][1], hvac_action="defrosting"))  # unit reverses valve
    ha.calls.clear()
    r = e.evaluate()
    assert r["lr"]["action"] == "hold" and e.st["last_change"].get("lr") == t0
    assert e.snapshot()["zones"][0]["override_until"] is None


def test_after_defrost_normal_logic_resumes():
    e, ha, clk = setup()
    e.evaluate()
    clk.t += 300
    act(ha, "climate.lr", "defrosting")
    e.evaluate()
    clk.t += 300
    act(ha, "climate.lr", "heating")
    r = e.evaluate()
    assert r["lr"]["reason"] == "no_change" and r["lr"]["defrost"] is None
    ev = [x[1:] for x in e.snapshot()["zones"][0]["events"]]
    assert ["defrost", "start"] in ev and ["defrost", "end"] in ev


def test_real_change_still_holds_when_not_defrosting():
    e, ha, clk = setup()
    e.evaluate()
    clk.t += 300
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75, hvac_action="heating"))
    e.evaluate()
    assert e.snapshot()["zones"][0]["override_until"]
    assert ["hold", "start"] in [x[1:] for x in e.snapshot()["zones"][0]["events"]]


# ---------- next change / why ----------
def test_next_change_afternoon_is_sleep_at_2200():
    e, ha, clk = setup()
    n = e.evaluate()["lr"]["next"]
    assert n["setpoint"] == 68.0 and n["profile"] == "sleep" and not n["preheat"]
    assert n["ts"] == NOW + (22 * 60 + 30 - 14 * 60) * 60


def test_next_change_before_wake_is_preheat_with_wake_time():
    e, ha, clk = setup(local=lt(5, 0))
    r = e.evaluate()
    assert r["lr"]["profile"] == "sleep"
    n = r["lr"]["next"]
    assert n["setpoint"] == 71.0 and n["preheat"]
    assert n["ts"] == NOW + 45 * 60 and n["wake_ts"] == NOW + 90 * 60  # pre-heat 5:45, 71 at 7:00


def test_preheat_event_and_flag():
    e, ha, clk = setup(local=lt(6, 0))
    r = e.evaluate()
    assert r["lr"].get("preheat") and ["preheat", "start"] in [x[1:] for x in e.snapshot()["zones"][0]["events"]]


def test_next_change_ignores_sun():
    e, ha, clk = setup(**{"sun.sun": ("above_horizon", {"elevation": 40, "azimuth": 225}),
                          "weather.w": ("sunny", {"temperature": 45, "cloud_coverage": 0})})
    n = e.evaluate()["lr"]["next"]
    assert n["ts"] == NOW + (8 * 60 + 30) * 60  # not "in 5 min" because of the solar offset


def test_no_next_on_hold_or_vacation():
    e, ha, clk = setup(**{VACATION_ENTITY: ("on", {})})
    r = e.evaluate()
    assert r["lr"]["next"] is None
    assert ["vacation", "start"] in [x[1:] for x in e.snapshot()["zones"][0]["events"]]
    e2, ha2, _ = setup()
    e2.start_override("lr")
    assert e2.evaluate()["lr"]["next"] is None


def test_events_window_and_cap():
    o = opts()
    o["zones"] = [dict(o["zones"][0], window="binary_sensor.w")] + o["zones"][1:]
    ha, clk = FakeHA(states(**{"binary_sensor.w": ("on", {})})), Clock()
    e = Engine(o, ha, None, clock=clk, localtime=lt14)
    for i in range(150):
        ha.s["binary_sensor.w"] = ("on" if i % 2 == 0 else "off", {})
        clk.t += 300
        e.evaluate()
    assert ["window", "start"] in [x[1:] for x in e.st["events"]["lr"]]
    assert len(e.st["events"]["lr"]) <= 200
    assert all(x[0] >= clk.t - 86400 for x in e.snapshot()["zones"][0]["events"])


# ---------- MA lows ----------
def test_legacy_lowered_option_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="smart_hvac"):
        e = Engine(opts(radiant_room_cap=80), FakeHA(states()), None, clock=lambda: NOW, localtime=lt14)
    assert e.radiant_setpoint_max == 85 and e.radiant_room_cap == 80  # still ignored
    assert any("radiant_room_cap=80 is ignored" in m for m in caplog.messages)
    assert Engine(opts(radiant_room_cap=70), FakeHA(states()), None).legacy_warnings()
    assert Engine(opts(), FakeHA(states()), None).legacy_warnings() == []
    assert Engine(opts(radiant_room_cap=85), FakeHA(states()), None).legacy_warnings() == []


def test_room_cap_alias_documented():
    doc = open(os.path.join(os.path.dirname(__file__), "..", "DOCS.md")).read()
    assert "radiant_room_cap" in doc and "deprecated alias" in doc


# ---------- presets API ----------
@pytest.fixture
def mk(aiohttp_client, monkeypatch):
    async def _mk(o):
        monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
        ha = FakeHA(states())
        e = Engine(o, ha, None, clock=lambda: NOW, localtime=lt14)
        return await aiohttp_client(make_app(e, interval=3600)), ha, e
    return _mk


def test_preset_map():
    assert PRESETS == {"home": "home", "sleep": "sleep", "away": "away", "eco": "auto"}


async def test_presets_one_tap(mk):
    c, ha, e = await mk(opts())
    for p, prof in PRESETS.items():
        r = await c.post("/api/preset", json={"preset": p.capitalize()})
        assert r.status == 200 and (await r.json())["profile"] == prof
    j = await (await c.post("/api/preset", json={"preset": "sleep"})).json()
    assert j["last"]["zones"]["lr"]["setpoint"] == 68.0
    assert (await c.post("/api/preset", json={"preset": "guest"})).status == 400
    assert (await c.post("/api/preset", data="x")).status == 400


async def test_state_has_next_and_events(mk):
    c, ha, e = await mk(opts())
    await c.post("/api/preset", json={"preset": "eco"})
    j = await (await c.get("/api/state")).json()
    assert j["last"]["zones"]["lr"]["next"]["setpoint"] == 68.0
    assert "events" in j["zones"][0] and j["radiant_setpoint_max"] == 85
