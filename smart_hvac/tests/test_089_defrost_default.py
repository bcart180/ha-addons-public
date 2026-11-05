"""0.8.9: defrost_aware defaults on. Reported or inferred defrost is not a short cycle or a missed setpoint."""
import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

from engine import DEFROST_GRACE_S  # noqa: E402
from server import load_options  # noqa: E402
from test_051_defrost_aware import room, setup, step  # noqa: E402

CFG = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def test_config_defaults_defrost_aware_on():
    raw = open(CFG).read()
    cfg = yaml.safe_load(raw)
    assert cfg["version"] == "0.8.9"
    assert cfg["options"]["defrost_aware"] is True
    assert "\n  defrost_aware: bool\n" in raw


def test_load_options_fills_default_and_keeps_explicit_false(tmp_path):
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"enabled": False}))
    assert load_options(str(missing))["defrost_aware"] is True
    off = tmp_path / "off.json"
    off.write_text(json.dumps({"defrost_aware": False}))
    assert load_options(str(off))["defrost_aware"] is False


def test_reported_defrosting_is_on_the_zone_payload():
    e, ha, clk = setup()
    room(ha, 66, "defrosting")
    r = step(e, clk, 60)
    assert r["lr"]["defrosting"] is True
    assert r["lr"]["defrost"] == "defrosting" and r["lr"]["defrost_inferred"] is False
    assert r["lr"]["reason"] == "defrosting"
    assert r["lr"]["action"] == "hold"


def test_inferred_defrost_on_fast_room_drop():
    e, ha, clk = setup()
    room(ha, 66.0, "heating")
    step(e, clk, 180)
    room(ha, 65.2, "heating")
    r = step(e, clk, 180)
    assert r["lr"]["defrosting"] is True and r["lr"]["defrost_inferred"] is True
    assert r["lr"]["defrost"] == "defrosting"


def test_inferred_when_heating_stops_and_the_room_drops():
    """Heads that never report defrosting go idle while the coil thaws."""
    e, ha, clk = setup()
    room(ha, 66.0, "heating")
    step(e, clk, 180)
    room(ha, 65.0, "idle")
    r = step(e, clk, 180)
    assert r["lr"]["defrosting"] is True and r["lr"]["defrost_inferred"] is True


def test_supply_drop_infers_when_the_room_holds():
    e, ha, clk = setup()
    room(ha, 66.0, "heating")
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, supply_temperature=100))
    step(e, clk, 180)
    room(ha, 66.0, "heating")
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, supply_temperature=98.5))
    r = step(e, clk, 180)
    assert r["lr"]["defrosting"] is True and r["lr"]["defrost_inferred"] is True


def test_cool_mode_is_not_an_inferred_defrost():
    e, ha, clk = setup()
    room(ha, 66.0, "heating")
    step(e, clk, 180)
    room(ha, 64.0, "heating")
    ha.s["climate.lr"] = ("cool", ha.s["climate.lr"][1])
    r = step(e, clk, 180)
    assert r["lr"]["defrost"] is None and r["lr"]["defrosting"] is False


def test_radiant_zone_is_not_inferred_even_if_marked_heat_pump():
    e, ha, clk = setup(heat_pump=False)
    # the bath zone is heat_only; flag it and drop its room the same way
    for z in e.opts["zones"]:
        if z["name"] == "bath":
            z["heat_pump"] = True
    ha.s["sensor.bath_t"] = ("80", {})
    ha.s["climate.bath"] = ("heat", dict(ha.s["climate.bath"][1], hvac_action="heating", current_temperature=80))
    step(e, clk, 180)
    ha.s["sensor.bath_t"] = ("76", {})
    ha.s["climate.bath"] = ("heat", dict(ha.s["climate.bath"][1], hvac_action="heating", current_temperature=76))
    r = step(e, clk, 180)
    assert r["bath"].get("defrosting") is False and r["bath"]["defrost"] is None


def test_defrost_during_a_hold_is_not_a_short_cycle():
    e, ha, clk = setup()
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75, hvac_action="heating"))
    step(e, clk, 60)
    held = e.snapshot()["zones"][0]["override_until"]
    assert held
    room(ha, 66, "defrosting")
    ha.calls.clear()
    r = step(e, clk, 60)
    assert r["lr"]["defrosting"] is True
    assert r["lr"]["reason"] != "short_cycle_guard"
    assert r["lr"]["action"] == "hold" and ha.calls == []
    assert e.snapshot()["zones"][0]["override_until"] == held


def test_grace_skips_short_cycle_then_the_guard_returns():
    e, ha, clk = setup()
    e.st["last_change"]["lr"] = clk.t
    room(ha, 66, "defrosting")
    step(e, clk, 120)
    room(ha, 66, "heating")
    assert step(e, clk, 60)["lr"]["defrosting"] is False
    ha.s["climate.lr"] = ("cool", dict(ha.s["climate.lr"][1], hvac_action="idle", temperature=71))
    ha.s["sensor.lr_t"] = ("66", {})
    r = step(e, clk, 60)
    assert r["lr"]["defrosting"] is False
    assert r["lr"]["reason"] != "short_cycle_guard"
    assert r["lr"]["action"] == "set" and r["lr"]["mode"] == "heat"
    # Past the grace, a real heat->cool decision is a short cycle again. The live mode still matches
    # the app's last write, so this is the guard and not a manual hold.
    clk.t += DEFROST_GRACE_S
    e.st["last_change"]["lr"] = clk.t - 120
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], hvac_action="idle", temperature=71))
    ha.s["sensor.lr_t"] = ("76", {})
    ha.s["weather.w"] = ("sunny", {"temperature": 85, "cloud_coverage": 100})
    r2 = step(e, clk, 30)
    assert r2["lr"]["reason"] == "short_cycle_guard"


def test_inferred_defrost_is_not_heat_delivered():
    e, ha, clk = setup(learned_preheat=True)
    room(ha, 70.8, "heating")  # within 1 F of the target: no warm-up run yet
    step(e, clk, 180)
    room(ha, 69.5, "heating")
    r = step(e, clk, 180)
    assert r["lr"]["defrosting"] is True and r["lr"]["defrost_inferred"] is True
    wu = e.st["warmup"]["lr"]
    assert wu.get("ep") is None and wu["samples"]["heat"] == []


def test_history_band_covers_inferred_defrost_without_recorder_action():
    e, ha, clk = setup()
    room(ha, 66.0, "heating")
    step(e, clk, 180)
    room(ha, 65.0, "heating")
    assert step(e, clk, 180)["lr"]["defrost_inferred"] is True
    step(e, clk, 60)  # the open band needs a later "now" than the start event
    ha.get_history = lambda ents, start: []
    h = e.history(e.zone("lr"), "24h")
    assert any(p[1] == 1 for p in h["defrost"])
    assert h["heating"] == []


def test_history_reported_defrost_is_not_drawn_as_heating():
    e, ha, clk = setup()
    e.evaluate()

    def hist(ents, start):
        return [[{"entity_id": "climate.lr", "state": "heat", "last_updated": "2027-01-15T07:00:00+00:00",
                  "attributes": {"temperature": 71, "hvac_action": "defrosting"}}]]

    ha.get_history = hist
    h = e.history(e.zone("lr"), "24h")
    assert [p[1] for p in h["heating"]] == [0]
    assert 1 in [p[1] for p in h["defrost"]]
