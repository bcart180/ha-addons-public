"""Optional split: radiant_setpoint_max vs radiant_air_shutoff (e.g. setpoint up to 85, air shutoff 80)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

from test_engine import eng, opts, states  # noqa: E402


def test_defaults_setpoint_max_85_air_shutoff_80_with_no_options():
    e, _ = eng(opts())  # options without either key: install needs no manual change
    assert e.radiant_setpoint_max == 85 and e.radiant_room_cap == 80
    assert e._cfg(e.zone("bath"))["radiant_room_cap"] == 80
    s = e.snapshot()
    assert s["radiant_setpoint_max"] == 85 and s["radiant_air_shutoff"] == 80 and s["radiant_room_cap"] == 80


def test_defaults_in_config_yaml():
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    assert "\n  radiant_setpoint_max: 85\n" in raw and "\n  radiant_air_shutoff: 80\n" in raw
    assert "\n  radiant_room_cap: 85\n" not in raw


def test_legacy_room_cap_saved_80_is_ignored_setpoint_still_85():
    e, _ = eng(opts(radiant_room_cap=80))  # what 0.3.2 saved live
    assert e.radiant_setpoint_max == 85 and e.radiant_room_cap == 80


def test_default_air_shutoff_80_turns_radiant_off_at_82_but_not_79():
    e, _ = eng(opts(), s=states(**{"sensor.bath_t": ("82", {})}))
    assert e.evaluate()["bath"]["reason"] == "overtemp_shutoff"
    e, _ = eng(opts(), s=states(**{"sensor.bath_t": ("79", {})}))
    assert e.evaluate()["bath"]["reason"] != "overtemp_shutoff"


def test_split_setpoint_85_air_shutoff_80():
    e, _ = eng(opts(radiant_setpoint_max=85, radiant_air_shutoff=80))
    assert e.radiant_setpoint_max == 85 and e.radiant_room_cap == 80
    s = e.snapshot()
    assert s["radiant_setpoint_max"] == 85 and s["radiant_air_shutoff"] == 80


def test_split_air_shutoff_80_turns_radiant_off_at_82():
    e, ha = eng(opts(radiant_setpoint_max=85, radiant_air_shutoff=80), s=states(**{"sensor.bath_t": ("82", {})}))
    assert e.evaluate()["bath"]["reason"] == "overtemp_shutoff"


def test_split_options_never_exceed_85():
    e, _ = eng(opts(radiant_setpoint_max=99, radiant_air_shutoff=99))
    assert e.radiant_setpoint_max == 85 and e.radiant_room_cap == 85


async def test_split_server_allows_85_setpoint_with_80_air_shutoff(aiohttp_client, monkeypatch):
    from server import make_app
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    e, _ = eng(opts(radiant_setpoint_max=85, radiant_air_shutoff=80))
    c = await aiohttp_client(make_app(e, interval=3600))
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 85})).status == 200
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 86})).status == 400
