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


def test_l1_frozen_after_setpoint_trips_no_rise():
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
    assert h.latched == "no_rise" and ha.sw == "off"
    assert c.t - t_reach < 50 * 60  # not a blind 2 h run
