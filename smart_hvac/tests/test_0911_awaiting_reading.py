"""0.9.11: a run with no fresh temperature report goes off as awaiting_reading (no latch) at 30 min, and the next
report decides it: a rise >= 1 F clears it, a smaller rise latches no_rise and notifies once."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import test_080_spare_heater as B  # noqa: E402

H = B.H


def blind_run(h, ha, c, temp=65.0):
    """Hourly sensor: the last report is the one just before the run; nothing new while the plug is on."""
    ha.temp = temp
    ha.seen = c.t
    h.tick()
    assert ha.sw == "on"
    start = c.t
    for _ in range(40 * 2):
        c.t += 30
        h.tick()
        if ha.sw == "off":
            break
    return c.t - start


def test_constants_unchanged_and_new_window():
    assert H.NO_FRESH_S == 30 * 60
    assert (H.NO_RISE_S, H.NO_RISE_F) == (45 * 60, 1.0)
    assert (H.MAX_RUN_S, H.COOLDOWN_S, H.DAY_LIMIT_S, H.HARD_CAP) == (2 * 3600, 20 * 60, 8 * 3600, 80.0)
    assert H.STALE_MIN_DEFAULT == 75


def test_no_report_for_30_min_goes_off_awaiting_without_latch_or_notify():
    h, ha, c = B.mk(temp=65.0)
    ran = blind_run(h, ha, c)
    assert 30 * 60 <= ran < 31 * 60
    assert ha.sw == "off" and h.last["reason"] == "awaiting_reading"
    assert h.latched is None and h.await_ref is not None and h.await_ref[1] == 65.0
    assert not ha.notes
    # stays off while waiting, even though the (old) reading is below setpoint - 1 and min-off has passed
    B.run(h, c, 20 * 60)
    assert ha.sw == "off" and h.last["reason"] == "awaiting_reading" and not ha.notes


def test_next_report_with_rise_clears_and_normal_control_resumes():
    h, ha, c = B.mk(temp=65.0)
    blind_run(h, ha, c)
    B.run(h, c, 10 * 60)
    ha.temp, ha.seen = 72.5, None           # the hourly report: +7.5 F
    c.t += 30
    h.tick()
    assert h.await_ref is None and h.latched is None and h.last["reason"] == "idle"
    assert ha.sw == "off" and not ha.notes
    ha.temp = 66.5                          # cools below setpoint - 1 (68 default): heats again
    B.run(h, c, 6 * 60)
    assert ha.sw == "on"


def test_next_report_without_1F_rise_latches_and_notifies_once():
    h, ha, c = B.mk(temp=65.0)
    blind_run(h, ha, c)
    B.run(h, c, 10 * 60)
    ha.temp, ha.seen = 65.9, None
    c.t += 30
    h.tick()
    assert h.latched == "no_rise" and h.await_ref is None and ha.sw == "off"
    assert h.last["reason"] == "no_rise"
    assert len([n for n in ha.notes if "rise" in n]) == 1
    B.run(h, c, 30 * 60)
    assert ha.sw == "off" and len([n for n in ha.notes if "rise" in n]) == 1
    h.set_mode("off")                       # the human reset still works
    h.set_mode("heat")
    assert h.latched is None and h.await_ref is None


def test_old_report_does_not_judge():
    h, ha, c = B.mk(temp=65.0)
    blind_run(h, ha, c)
    ha.temp, ha.seen = 75.0, h.await_ref[0] - 60   # value reported before the off
    B.run(h, c, 60)
    assert h.await_ref is not None and h.last["reason"] == "awaiting_reading" and ha.sw == "off"


def test_fresh_flat_reports_still_latch_at_45_min():
    """A sensor that does report, and shows no rise, is still a real fault: no_rise at 45 min as before."""
    h, ha, c = B.mk(temp=60.0)
    h.tick()
    assert ha.sw == "on"
    B.run(h, c, 44 * 60)
    assert ha.sw == "on" and h.await_ref is None
    B.run(h, c, 2 * 60)
    assert h.latched == "no_rise" and ha.sw == "off"


def test_report_inside_window_keeps_it_running():
    """A report 20 min in (age < 30 min at the 30 min mark) keeps the run going; no awaiting_reading."""
    h, ha, c = B.mk(temp=65.0)
    ha.seen = c.t
    h.tick()
    B.run(h, c, 20 * 60)
    ha.seen = c.t                           # one report at 20 min, value unchanged
    B.run(h, c, 15 * 60)
    assert ha.sw == "on" and h.await_ref is None


def test_blind_at_or_above_setpoint_is_plain_holding():
    h, ha, c = B.mk(temp=66.0)
    ha.seen = c.t
    h.tick()
    assert ha.sw == "on"
    ha.temp = 68.2                          # a report at setpoint, then nothing (frozen)
    ha.seen = c.t + 30
    B.run(h, c, 31 * 60)
    assert ha.sw == "off" and h.await_ref is None and h.latched is None
    assert h.last["reason"] in ("holding", "satisfied", "idle")


def test_stale_sensor_while_awaiting_still_fails_off_and_notifies():
    h, ha, c = B.mk(temp=65.0)
    blind_run(h, ha, c)
    B.run(h, c, 50 * 60)                    # report now > 75 min old
    assert h.last["reason"] == "sensor_unavailable" and ha.sw == "off"
    assert any("stale" in n or "unavailable" in n for n in ha.notes)
    ha.temp, ha.seen = 70.0, None           # sensor back: judged, rise >= 1 F
    c.t += 30
    h.tick()
    assert h.await_ref is None and h.latched is None


def test_cap_still_wins_while_awaiting():
    h, ha, c = B.mk(temp=65.0)
    blind_run(h, ha, c)
    ha.temp, ha.seen = 80.5, None
    c.t += 30
    h.tick()
    assert h.last["reason"] == "hard_cap" and ha.sw == "off"
