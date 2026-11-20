"""0.8.4: SA R1 (safety state survives restart) and SA L1 (setpoint reach does not exempt the progress rule)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import test_080_spare_heater as B  # noqa: E402

H = B.H


def mkp(tmp_path, temp=65.0, clock=None, ha=None):
    c = clock or B.Clock()
    ha = ha or B.FakeHA(c, temp)
    sb = {"heater_switch": B.SW, "temp_sensor": B.TS, "enabled": True, "dry_run": False}
    opts = {"enabled": True, "dry_run": False, "spare_bathroom": sb}
    return H.SpareHeater(opts, ha, str(tmp_path / "heater.json"), clock=c), ha, c


def restart(tmp_path, c, ha):
    h2, _, _ = mkp(tmp_path, clock=c, ha=ha)
    h2.safe_off("startup")
    return h2


def test_restart_while_latched_stays_latched(tmp_path):
    h, ha, c = mkp(tmp_path, temp=65.0)
    B.run(h, c, 50 * 60)  # frozen reading -> no_rise
    assert h.latched == "no_rise" and ha.sw == "off"
    h2 = restart(tmp_path, c, ha)
    assert h2.latched == "no_rise"
    B.run(h2, c, 10 * 60)
    assert ha.sw == "off" and h2.last["reason"] == "no_rise"


def test_restart_mid_cooldown_keeps_cooldown(tmp_path):
    h, ha, c = mkp(tmp_path, temp=60.0)
    h.tick()
    for i in range(240):  # keep rising so no_rise never trips; 2 h max run -> cooldown
        c.t += 30
        ha.temp = 60.0 + (i // 60) * 1.0
        h.tick()
    assert h.last["reason"] == "cooldown" and ha.sw == "off"
    cu = h.cooldown_until
    c.t += 5 * 60
    h2 = restart(tmp_path, c, ha)
    assert h2.cooldown_until == cu
    h2.tick()
    assert ha.sw == "off" and h2.last["reason"] == "cooldown"


def test_24h_history_survives_restart(tmp_path):
    h, ha, c = mkp(tmp_path, temp=60.0)
    h.runs = [(c.t - 9 * 3600, c.t - 60)]
    h._save()
    h2 = restart(tmp_path, c, ha)
    assert h2.runs == [(c.t - 9 * 3600, c.t - 60)]
    c.t += 30
    h2.tick()
    assert h2.latched == "lockout" and ha.sw == "off"


def test_corrupt_file_fails_safe(tmp_path):
    p = tmp_path / "heater.json"
    p.write_text("{not json")
    c = B.Clock()
    ha = B.FakeHA(c, 60.0)
    ha.sw = "on"
    h2 = restart(tmp_path, c, ha)
    assert ha.sw == "off" and h2.latched is None
    assert h2.cooldown_until == c.t + H.COOLDOWN_S
    B.run(h2, c, 19 * 60)
    assert ha.sw == "off"
    B.run(h2, c, 2 * 60)
    assert ha.sw == "on"


def test_missing_file_defaults(tmp_path):
    h, ha, c = mkp(tmp_path)
    assert h.latched is None and h.cooldown_until == 0.0 and h.runs == []


def test_atomic_write_no_tmp_left(tmp_path):
    h, ha, c = mkp(tmp_path, temp=60.0)
    h.tick()
    d = json.loads((tmp_path / "heater.json").read_text())
    assert d["on_since"] == c.t and d["rise_ref"][1] == 60.0
    assert not (tmp_path / "heater.json.tmp").exists()


def test_l1_frozen_at_setpoint_holds_off_without_latch():
    h, ha, c = B.mk(temp=66.0)
    h.tick()
    assert ha.sw == "on"
    c.t += 10 * 60
    ha.temp = 68.0  # reaches setpoint (68) -> checkpoint reset once
    h.tick()
    assert h.rise_ref == (c.t, 68.0)
    t_reach = c.t
    B.run(h, c, 44 * 60)
    assert ha.sw == "on"
    B.run(h, c, 2 * 60)
    assert ha.sw == "off" and h.latched is None and h.last["reason"] in ("holding", "idle")
    assert c.t - t_reach < 50 * 60  # not a blind 2 h run
    ha.temp = 66.5  # drops below setpoint - 1 -> heats again (no latch to reset)
    B.run(h, c, 6 * 60)
    assert ha.sw == "on"


def test_no_rise_below_setpoint_still_latches():
    h, ha, c = B.mk(temp=60.0)
    B.run(h, c, 50 * 60)
    assert h.latched == "no_rise" and ha.sw == "off"


def write(tmp_path, d):
    (tmp_path / "heater.json").write_text(json.dumps(d))


def test_m1_future_timestamps_dropped_and_cooldown_clamped(tmp_path):
    c = B.Clock()
    write(tmp_path, {"mode": "heat", "setpoint": 68, "on_since": c.t + 3600, "off_since": c.t + 999,
                     "cooldown_until": c.t + 10 * 86400, "rise_ref": [c.t + 3600, 60],
                     "runs": [[c.t - 100, c.t - 200], [c.t - 500, c.t + 9999], [c.t - 900, c.t - 600]]})
    h, _ha, _ = mkp(tmp_path, clock=c)
    assert h.on_since is None and h.off_since is None and h.rise_ref is None
    assert h.cooldown_until == c.t + H.COOLDOWN_S
    assert h.runs == [(c.t - 900, c.t - 600)]


def test_m1_backward_clock_jump_never_negative_and_lockout_enforced(tmp_path):
    h, ha, c = mkp(tmp_path, temp=60.0)
    h.runs = [(c.t - 9 * 3600, c.t - 120)]
    h.on_since = c.t + 3600  # clock jumped backward while on
    assert h._on_seconds_24h(c.t) >= 9 * 3600 - 120
    h2, _, _ = mkp(tmp_path, temp=60.0)
    h2.on_since = c.t + 3600
    assert h2._on_seconds_24h(c.t) == 0
    c.t += 30
    ha.sw = "on"
    h.commanded = True
    h.tick()
    assert h.latched == "lockout" and ha.sw == "off"
    assert all(b >= a for a, b in h.runs)


def test_l2_restart_loop_keeps_max_run_timer(tmp_path):
    h, ha, c = mkp(tmp_path, temp=60.0)
    h.tick()
    assert ha.sw == "on"
    t0 = c.t
    for _ in range(5):  # restart every ~25 min while heating, temp keeps rising 1 F per restart
        B.run(h, c, 25 * 60)
        ha.temp += 1.0
        h = restart(tmp_path, c, ha)
        c.t += 30
        h.tick()
    B.run(h, c, 10 * 60)
    # 2 h after the ORIGINAL start the run must have ended in cooldown
    assert c.t - t0 >= H.MAX_RUN_S and ha.sw == "off" and h.cooldown_until > c.t - 20 * 60


def test_l2_restart_after_max_run_starts_cooldown(tmp_path):
    c = B.Clock()
    write(tmp_path, {"mode": "heat", "setpoint": 68, "on_since": c.t - 2 * 3600 - 60, "runs": []})
    ha = B.FakeHA(c, 60.0)
    h = restart(tmp_path, c, ha)
    assert h.cooldown_until == c.t + H.COOLDOWN_S
    c.t += 30
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "cooldown"


def test_l3_corrupt_file_alerts(tmp_path):
    (tmp_path / "heater.json").write_text("\x00garbage")
    c = B.Clock()
    ha = B.FakeHA(c, 60.0)
    restart(tmp_path, c, ha)
    assert any("unreadable" in n for n in ha.notes) and ha.sw == "off"


def test_0911_awaiting_survives_restart_and_is_judged(tmp_path):
    h, ha, c = mkp(tmp_path, temp=65.0)
    ha.seen = c.t
    h.tick()
    B.run(h, c, 31 * 60)
    assert h.last["reason"] == "awaiting_reading" and ha.sw == "off"
    d = json.load(open(tmp_path / "heater.json"))
    assert d["await_ref"][1] == 65.0 and d["latched"] is None
    h2 = restart(tmp_path, c, ha)
    assert h2.await_ref is not None
    B.run(h2, c, 5 * 60)
    assert ha.sw == "off" and h2.last["reason"] == "awaiting_reading"
    ha.temp, ha.seen = 65.5, None           # next report: no 1 F rise -> latch + notify
    c.t += 30
    h2.tick()
    assert h2.latched == "no_rise" and any("rise" in n for n in ha.notes)
    assert json.load(open(tmp_path / "heater.json"))["await_ref"] is None


def test_0911_await_ref_dropped_when_latched_or_bad(tmp_path):
    write(tmp_path, {"mode": "heat", "setpoint": 67.0, "latched": "lockout", "await_ref": [B.T0 - 60, 65.0]})
    c = B.Clock()
    h, _, _ = mkp(tmp_path, clock=c)
    assert h.await_ref is None
    write(tmp_path, {"mode": "heat", "setpoint": 67.0, "await_ref": [B.T0 + 9999, 65.0]})
    h, _, _ = mkp(tmp_path, clock=c)
    assert h.await_ref is None
    write(tmp_path, {"mode": "heat", "setpoint": 67.0, "await_ref": [B.T0 - 60, True]})
    h, _, _ = mkp(tmp_path, clock=c)
    assert h.await_ref is None


def test_0911_rise_ref_rearmed_after_failed_off_mid_run():
    """A failed off write mid-run (HA down) clears the checkpoint but the plug stays on. 0.9.11 re-arms it when
    the plug is seen on, so the run is still bounded by awaiting_reading / no_rise, not only the 2 h max run."""
    h, ha, c = B.mk(temp=65.0)
    ha.seen = c.t
    h.tick()
    assert ha.sw == "on"
    B.run(h, c, 5 * 60)
    real = ha.heater_switch

    def broken(eid, on):
        raise OSError("ha down")

    ha.fail_states, ha.heater_switch = True, broken
    c.t += 30
    h.tick()                                # tick fails -> safe_off -> off write fails; plug still on
    assert ha.sw == "on" and h.rise_ref is None
    ha.fail_states, ha.heater_switch = False, real
    ha.seen = c.t                           # one report as HA comes back, then nothing
    c.t += 30
    h.tick()
    assert h.rise_ref is not None
    B.run(h, c, 31 * 60)
    assert ha.sw == "off" and h.last["reason"] == "awaiting_reading"
