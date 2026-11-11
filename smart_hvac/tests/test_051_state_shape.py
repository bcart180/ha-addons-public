"""MA L1 (0.5.1): a state file that parses but has the wrong shape resets the bad parts, never errors."""
import json
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

from engine import Engine  # noqa: E402
from test_040 import Clock  # noqa: E402
from test_engine import ZONES, FakeHA, lt14, opts, states  # noqa: E402


def make(tmp_path, data, **o):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(data))
    zones = [dict(z, heat_pump=True) for z in ZONES]
    e = Engine(opts(zones=zones, **o), FakeHA(states()), str(p), clock=Clock(), localtime=lt14)
    return e


@pytest.mark.parametrize("data", [[], [1, 2], "x", 5, None, True])
def test_top_level_not_object(tmp_path, data):
    e = make(tmp_path, data, defrost_aware=True)
    for _ in range(3):
        e.evaluate()
    assert e.st["profile"] == "auto"


BAD_TOP = {k: v for k, v in [("last_change", []), ("last_cmd", "x"), ("override_until", 3), ("history", None),
                              ("last_result", []), ("events", "e"), ("active", 1), ("defrost_since", []),
                              ("window", "w"), ("window_saved", 0), ("defrost_aware", []), ("profile", 7)]}


@pytest.mark.parametrize("aware", [False, True])
def test_wrong_top_level_types(tmp_path, aware, caplog):
    caplog.set_level(logging.DEBUG, logger="smart_hvac")
    e = make(tmp_path, BAD_TOP, defrost_aware=aware)
    for k in BAD_TOP:
        if k != "profile":
            assert isinstance(e.st[k], dict), k
    assert e.st["profile"] == "auto"
    for _ in range(3):
        e.evaluate()
    e.save()
    assert "not an object; reset" in caplog.text


def test_malformed_zone_entries(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="smart_hvac")
    z = ZONES[0]["name"]
    data = {"last_change": {z: "soon"}, "last_cmd": {z: [1]}, "override_until": {z: {"a": 1}},
            "history": {z: "h"}, "events": {z: [[None, "hold", "start"]]}, "active": {z: [1]},
            "defrost_since": {z: "t"}, "window": {z: 1}, "window_saved": {z: []},
            "defrost_aware": {z: "bad"}, "last_result": {"zones": {z: "bad"}}}
    e = make(tmp_path, data, defrost_aware=True)
    for k in data:
        if k != "last_result":
            assert z not in e.st[k] or k == "defrost_aware", k
    assert z not in e.st["last_result"]["zones"]
    for _ in range(3):
        e.evaluate()
    assert "malformed" in caplog.text


def test_last_result_zones_not_object(tmp_path):
    e = make(tmp_path, {"last_result": {"zones": [1]}})
    assert e.st["last_result"] == {}
    e.evaluate()


def test_malformed_defrost_samples(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="smart_hvac")
    z = ZONES[0]["name"]
    good = [100.0, 70.0, "heating"]
    data = {"defrost_aware": {z: {"samples": [good, [1], "x", [None, 70, "heating"], [1, 2, 3]],
                                  "episodes": [5, "x", None], "since": "now", "exempt": "yes"}}}
    e = make(tmp_path, data, defrost_aware=True)
    d = e.st["defrost_aware"][z]
    assert d == {"samples": [good], "episodes": [5], "since": None, "exempt": False}
    for _ in range(3):
        e.evaluate()
    assert "malformed defrost_aware entry" in caplog.text


def test_defrost_entry_wrong_containers(tmp_path):
    z = ZONES[0]["name"]
    e = make(tmp_path, {"defrost_aware": {z: {"samples": "s", "episodes": 3}}}, defrost_aware=True)
    assert e.st["defrost_aware"][z] == {"samples": [], "episodes": [], "since": None, "exempt": False}
    e.evaluate()


def test_window_slope_block_and_malformed_reset(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="smart_hvac")
    e = make(tmp_path, {"window_slope": {"active": "yes", "samples": {"lr": "no"}, "saved": 4,
                                         "below_since": {"lr": "soon"}, "pause_starts": {"lr": [1, "x"]}}})
    assert e.st["window_slope"]["active"] is False
    assert e.st["window_slope"]["samples"] == {}
    assert e.st["window_slope"]["pause_starts"]["lr"] == [1]
    e.evaluate()
    block = e.snapshot()["zones"][0]["window_slope"]
    assert set(block) == {"active", "since", "max_until", "slope_f_per_h", "trigger_zone"}
    assert block["active"] is False and block["slope_f_per_h"] is None
    assert block["since"] is None and block["max_until"] is None and block["trigger_zone"] is None


def test_well_formed_state_unchanged(tmp_path):
    e = make(tmp_path, {}, defrost_aware=True)
    for _ in range(3):
        e.evaluate()
    e.save()
    before = json.loads((tmp_path / "state.json").read_text())
    e2 = Engine(e.opts, FakeHA(states()), str(tmp_path / "state.json"), clock=Clock(), localtime=lt14)
    before["app_off"] = []
    assert e2.st == before
