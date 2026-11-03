"""0.8.5 SA M1b: heater durations run on a monotonic clock; wall clock jumps force OFF + 20 min cooldown."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import test_080_spare_heater as B  # noqa: E402

H = B.H


class Mono:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def mkj(temp=60.0, tmp=None):
    w, m = B.Clock(), Mono()
    ha = B.FakeHA(w, temp)
    sb = {"heater_switch": B.SW, "temp_sensor": B.TS, "enabled": True, "dry_run": False}
    opts = {"enabled": True, "dry_run": False, "spare_bathroom": sb}
    h = H.SpareHeater(opts, ha, tmp, clock=w, mono=m)
    return h, ha, w, m


def step(h, ha, w, m, s=30, rise=None):
    w.t += s
    m.t += s
    if rise is not None:
        ha.temp = rise
    return h.tick()


def heat_for(h, ha, w, m, secs):
    """Heat with a slowly rising reading so no_rise never trips."""
    base = ha.temp
    for i in range(secs // 30):
        step(h, ha, w, m, rise=base + (i // 60) * 1.0)


@pytest.mark.parametrize("jump", [86400, 3600, 600])
def test_backward_jump_goes_off_within_one_cycle(jump):
    h, ha, w, m = mkj()
    h.tick()
    heat_for(h, ha, w, m, 30 * 60)
    assert ha.sw == "on"
    w.t -= jump  # wall clock goes back mid-run
    m.t += 30
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "clock_jump"
    assert any("clock jumped" in n for n in ha.notes)
    # 20 min cooldown on the monotonic timeline, then it may resume
    for _ in range(19 * 2):
        step(h, ha, w, m)
        assert ha.sw == "off"
    for _ in range(4):
        step(h, ha, w, m)
    assert ha.sw == "on"


@pytest.mark.parametrize("jump", [86400, 3600, 600])
def test_backward_jump_cannot_stretch_2h_run(jump):
    """Simulates SA's attack: jump back after the cooldown, keep heating; max run still 2 h of real time."""
    h, ha, w, m = mkj()
    h.tick()
    on_streak = longest = 0
    for i in range(10 * 120):
        step(h, ha, w, m, rise=60.0 + (i % 240) // 60)
        if i % 100 == 50:
            w.t -= jump
        on_streak = on_streak + 30 if ha.sw == "on" else 0
        longest = max(longest, on_streak)
    assert longest <= H.MAX_RUN_S


def test_small_wall_drift_is_not_a_jump():
    h, ha, w, m = mkj()
    h.tick()
    for _ in range(20):
        w.t += 30 + 2  # NTP slew of a couple of seconds per tick
        m.t += 30
        h.tick()
    assert ha.sw == "on" and h.last["reason"] == "heating"


def test_forward_jump_does_not_extend_run_and_goes_off():
    h, ha, w, m = mkj()
    h.tick()
    heat_for(h, ha, w, m, 30 * 60)
    on24 = h._on_seconds_24h(h._now())
    w.t += 6 * 3600  # wall clock leaps ahead
    m.t += 30
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "clock_jump"
    # run time counted on monotonic time: 30 s more, not 6 h more, and no false lockout
    assert h._on_seconds_24h(h._now()) <= on24 + 60
    assert h.latched is None
    assert sum(b - a for a, b in h.runs) <= 31 * 60


def test_forward_jump_does_not_shorten_cooldown():
    h, ha, w, m = mkj()
    h.tick()
    heat_for(h, ha, w, m, 2 * 3600 + 60)
    assert h.last["reason"] == "cooldown" and ha.sw == "off"
    w.t += 3600  # would end a wall-clock cooldown instantly
    m.t += 30
    h.tick()
    for _ in range(10):
        step(h, ha, w, m)
        assert ha.sw == "off"


def test_heartbeat_and_staleness_use_wall_clock():
    h, ha, w, m = mkj()
    h.tick()
    step(h, ha, w, m)
    assert h.last["ts"] == w.t  # watchdog compares heartbeat to HA wall time
    ha.seen = w.t - 80 * 60  # sensor last reported 80 min ago (wall)
    step(h, ha, w, m)
    assert h.last["reason"] == "sensor_unavailable" and ha.sw == "off"


def test_persisted_wall_timestamps_still_sanitized(tmp_path):
    p = tmp_path / "heater.json"
    h, ha, w, m = mkj(tmp=str(p))
    h.tick()
    heat_for(h, ha, w, m, 10 * 60)
    h.safe_off("shutdown")
    d = json.loads(p.read_text())
    d["runs"].append([w.t + 9999, w.t + 99999])  # future run: dropped on load
    d["cooldown_until"] = w.t + 10 * 86400  # clamped to 20 min
    p.write_text(json.dumps(d))
    h2, _, _, _ = mkj(tmp=str(p))
    # fresh clocks restart at T0, so the saved run (ending ~T0 + 10 min) is also "future" and dropped
    assert all(b <= B.T0 + H.FUTURE_SKEW_S for _, b in h2.runs)
    assert h2.cooldown_until <= B.T0 + H.COOLDOWN_S + 1
