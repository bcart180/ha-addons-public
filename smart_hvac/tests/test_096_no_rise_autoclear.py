"""0.9.6: the no_rise latch clears itself on a fresh post-trip reading >= 1 F above the trip checkpoint."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import test_080_spare_heater as B  # noqa: E402
import test_084_heater_persist as P  # noqa: E402

H = B.H


def trip(h, ha, c, temp=65.0):
    """Heat with a reading frozen in time (slow sensor): no report during the run -> no_rise latch."""
    ha.temp = temp
    h.tick()
    assert ha.sw == "on"
    ha.seen = c.t  # sensor's last report stays at run start
    for _ in range(46 * 2):
        c.t += 30
        h.tick()
        if h.latched:
            break
    assert h.latched == "no_rise" and ha.sw == "off"
    return h.nr_trip


def test_fresh_reading_1F_above_clears_without_notify():
    h, ha, c = B.mk(temp=65.0)
    t = trip(h, ha, c)
    assert t is not None and t[1] == 65.0
    B.run(h, c, 10 * 60)                       # still no new report (56 min old, not stale): stays latched
    assert h.latched == "no_rise"
    ha.temp, ha.seen = 73.5, None              # fresh report after the trip, +8.5 F
    c.t += 30
    h.tick()
    assert h.latched is None and h.nr_trip is None and ha.sw == "off"
    assert not ha.notes
    assert h.last["reason"] == "idle"
    # normal hysteresis afterwards: on at setpoint - 1 (68 default -> 67)
    ha.temp = 67.0
    B.run(h, c, 6 * 60)
    assert ha.sw == "on"


def test_fresh_reading_without_1F_rise_stays_latched():
    h, ha, c = B.mk(temp=65.0)
    trip(h, ha, c)
    ha.temp, ha.seen = 65.9, None
    B.run(h, c, 3 * 3600)
    assert h.latched == "no_rise" and ha.sw == "off"
    assert any("rise" in n for n in ha.notes)   # post-trip reading did not prove a rise: real fault
    ha.temp = 60.0                             # dropped, but never proved a rise: still latched
    B.run(h, c, 600)
    assert h.latched == "no_rise" and ha.sw == "off"


def test_old_reading_does_not_clear():
    h, ha, c = B.mk(temp=65.0)
    t = trip(h, ha, c)
    ha.temp, ha.seen = 70.0, t[0] - 60        # value reported before the trip time
    c.t += 30
    h.tick()
    assert h.latched == "no_rise"


def test_existing_frozen_value_test_still_latches():
    h, ha, c = B.mk(temp=60)
    h.tick()
    ha.temp = 60.9
    B.run(h, c, 45 * 60)
    assert h.latched == "no_rise"
    B.run(h, c, 3600)
    assert ha.sw == "off" and h.latched == "no_rise"


def test_trip_persists_and_clears_after_restart(tmp_path):
    h, ha, c = P.mkp(tmp_path, temp=65.0)
    trip(h, ha, c)
    d = json.load(open(tmp_path / "heater.json"))
    assert d["latched"] == "no_rise" and d["nr_trip"][1] == 65.0
    h2 = P.restart(tmp_path, c, ha)
    assert h2.latched == "no_rise" and h2.nr_trip is not None
    ha.temp, ha.seen = 67.0, None
    c.t += 30
    h2.tick()
    assert h2.latched is None
    assert json.load(open(tmp_path / "heater.json"))["nr_trip"] is None


def test_legacy_latch_without_trip_info_needs_manual_reset(tmp_path):
    p = tmp_path / "heater.json"
    p.write_text(json.dumps({"mode": "heat", "setpoint": 67.0, "latched": "no_rise"}))
    c = B.Clock()
    ha = B.FakeHA(c, 73.5)
    h2 = P.restart(tmp_path, c, ha)
    B.run(h2, c, 600)
    assert h2.latched == "no_rise" and ha.sw == "off"
    h2.set_mode("off")
    h2.set_mode("heat")
    ha.temp = 66.0
    B.run(h2, c, 6 * 60)
    assert h2.latched is None and ha.sw == "on"


def test_mode_off_clears_trip():
    h, ha, c = B.mk(temp=65.0)
    trip(h, ha, c)
    h.set_mode("off")
    assert h.latched is None and h.nr_trip is None


def test_lockout_and_relay_fault_not_auto_cleared():
    h, ha, c = B.mk(temp=65.0)
    h.latched, h.nr_trip = "lockout", (c.t - 60, 60.0)
    B.run(h, c, 60)
    assert h.latched == "lockout"
