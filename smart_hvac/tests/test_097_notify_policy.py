"""0.9.7: spare bathroom heater notifies only on real problems, not routine cycle events."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import test_080_spare_heater as B  # noqa: E402
import test_084_heater_persist as P  # noqa: E402
import test_096_no_rise_autoclear as A  # noqa: E402


def test_trip_then_auto_clear_is_silent():
    """The hourly-sensor pattern: trip after 45 min, later reading shows the room rose, no notification."""
    h, ha, c = B.mk(temp=65.0)
    A.trip(h, ha, c)
    assert h.latched == "no_rise" and not ha.notes
    ha.temp, ha.seen = 72.1, None
    c.t += 30
    h.tick()
    assert h.latched is None and h.last["reason"] == "idle" and not ha.notes


def test_post_trip_reading_without_rise_notifies_once():
    h, ha, c = B.mk(temp=65.0)
    A.trip(h, ha, c)
    assert not ha.notes
    ha.temp, ha.seen = 65.4, None
    c.t += 30
    h.tick()
    assert h.latched == "no_rise"
    assert len([n for n in ha.notes if "rise" in n]) == 1
    B.run(h, c, 600)
    assert len([n for n in ha.notes if "rise" in n]) == 1  # once per episode


def test_waiting_for_next_reading_stays_quiet():
    """Frozen last-report (slow sensor) after the trip: still inside the stale window, no notify yet."""
    h, ha, c = B.mk(temp=65.0)
    A.trip(h, ha, c)
    B.run(h, c, 10 * 60)  # 55 min old, stale cutoff is 75
    assert h.latched == "no_rise" and not ha.notes


def test_legacy_latch_without_trip_info_notifies(tmp_path):
    p = tmp_path / "heater.json"
    p.write_text(json.dumps({"mode": "heat", "setpoint": 67.0, "latched": "no_rise"}))
    c = B.Clock()
    ha = B.FakeHA(c, 73.5)
    h2 = P.restart(tmp_path, c, ha)
    h2.tick()
    assert h2.latched == "no_rise" and any("rise" in n for n in ha.notes)


def test_two_hour_cooldown_is_log_only():
    h, ha, c = B.mk(temp=60)
    h.tick()
    h.on_since = c.t - 2 * 3600
    h.rise_ref = (c.t, 50)
    B.run(h, c, 30)
    assert h.last["reason"] == "cooldown" and not ha.notes


def test_real_faults_still_notify():
    h, ha, c = B.mk(temp=80)
    h.tick()
    assert any("cap" in n or "80" in n for n in ha.notes)

    h, ha, c = B.mk(temp="unavailable")
    h.tick()
    assert any("stale" in n or "unavailable" in n for n in ha.notes)

    h, ha, c = B.mk(temp=70)
    h.tick()
    ha.sw = "on"
    B.run(h, c, 30)
    assert any("should be off" in n for n in ha.notes)
