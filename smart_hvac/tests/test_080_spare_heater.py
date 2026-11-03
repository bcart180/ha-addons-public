"""0.8.0 spare bathroom heater: every safety path with fake entities and a fake clock."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import heater as H  # noqa: E402

T0 = 1_800_000_000
SW, TS, PW, DR = "switch.fake_plug_555", "sensor.fake_temp_555", "sensor.fake_power_555", "binary_sensor.fake_door_555"


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeHA:
    """Fake HA: switch follows commands; temp sensor reported 'now' unless frozen."""
    def __init__(self, clock, temp=65.0, power=None, door=None):
        self.c, self.temp, self.power, self.door = clock, temp, power, door
        self.sw = "off"
        self.seen = None  # None = fresh every read
        self.cmds, self.notes, self.pub = [], [], []
        self.fail_states = False

    def get_states(self):
        if self.fail_states:
            raise OSError("ha down")
        s = [{"entity_id": SW, "state": self.sw},
             {"entity_id": TS, "state": str(self.temp), "last_reported": iso(self.seen if self.seen else self.c())}]
        if self.power is not None:
            s.append({"entity_id": PW, "state": str(self.power)})
        if self.door is not None:
            s.append({"entity_id": DR, "state": self.door})
        return s

    def heater_switch(self, eid, on):
        assert eid == SW
        self.cmds.append(on)
        self.sw = "on" if on else "off"

    def heater_notify(self, title, msg, svc):
        self.notes.append(msg)

    def publish_state(self, eid, st, attrs):
        assert eid == "climate.spare_bathroom"
        self.pub.append((st, attrs))


def mk(temp=65.0, power=None, door=None, live=True, **cfg):
    c = Clock()
    ha = FakeHA(c, temp, power, door)
    sb = {"heater_switch": SW, "temp_sensor": TS}
    if power is not None:
        sb["power_sensor"] = PW
    if door is not None:
        sb["door_sensor"] = DR
    sb.update({"enabled": True, "dry_run": not live})  # 0.8.1 heater-only gates
    sb.update(cfg)
    opts = {"enabled": True, "dry_run": not live, "spare_bathroom": sb}
    return H.SpareHeater(opts, ha, None, clock=c), ha, c


def run(h, c, secs, step=30):
    for _ in range(int(secs // step)):
        c.t += step
        h.tick()


# ---------- config / defaults ----------
def test_unset_is_disabled_and_never_writes():
    ha = FakeHA(Clock())
    o = {"enabled": True, "dry_run": False, "spare_bathroom": {"heater_switch": "", "temp_sensor": ""}}
    h = H.SpareHeater(o, ha)
    assert not h.configured
    h.tick()
    h.safe_off()
    assert ha.cmds == [] and ha.pub == []


def test_bad_entity_ids_are_ignored():
    cfg = H.config({"spare_bathroom": {"heater_switch": "climate.room_a", "temp_sensor": "sensor.ok_1",
                                       "notify_service": "script.x"}})
    assert cfg["heater_switch"] == "" and cfg["temp_sensor"] == "sensor.ok_1" and cfg["notify_service"] == ""


def test_defaults_and_setpoint_clamp():
    h, _, _ = mk()
    assert h.st == {"mode": "heat", "setpoint": 68.0}
    h.set_setpoint(90)
    assert h.st["setpoint"] == 75
    h.set_setpoint(40)
    assert h.st["setpoint"] == 55
    with pytest.raises(ValueError):
        h.set_setpoint("x")
    with pytest.raises(ValueError):
        h.set_mode("cool")


# ---------- hysteresis + min on/off ----------
def test_hysteresis_on_at_minus_1_off_at_plus_half():
    h, ha, c = mk(temp=67.5)
    h.tick()
    assert ha.sw == "off"          # 67.5 > 67: no call for heat
    ha.temp = 67.0
    run(h, c, 30)
    assert ha.sw == "on"
    ha.temp = 68.4
    run(h, c, 600)
    assert ha.sw == "on"           # below 68.5
    ha.temp = 68.5
    run(h, c, 30)
    assert ha.sw == "off"


def test_min_on_and_min_off_5_minutes():
    h, ha, c = mk(temp=60)
    h.tick()
    assert ha.sw == "on"
    ha.temp = 70
    run(h, c, 240)
    assert ha.sw == "on"           # 4 min < min-on
    run(h, c, 60)
    assert ha.sw == "off"
    ha.temp = 60
    run(h, c, 240)
    assert ha.sw == "off"          # min-off
    assert h.last["reason"] == "min_off"
    run(h, c, 60)
    assert ha.sw == "on"


def test_dry_run_and_master_off_never_turn_on():
    for live in (False,):
        h, ha, c = mk(temp=50, live=live)
        run(h, c, 600)
        assert True not in ha.cmds and h.last["reason"] == "dry_run"
    h, ha, c = mk(temp=50)
    h.opts["enabled"] = False
    run(h, c, 60)
    assert True not in ha.cmds and h.last["reason"] == "app_off"


def test_mode_off_turns_plug_off():
    h, ha, c = mk(temp=60)
    h.tick()
    assert ha.sw == "on"
    h.set_mode("off")
    run(h, c, 30)
    assert ha.sw == "off" and h.last["reason"] == "mode_off"


# ---------- hard cap ----------
def test_hard_cap_80_latched_until_below_setpoint():
    h, ha, c = mk(temp=60)
    h.tick()
    ha.temp = 80
    run(h, c, 30)                  # min-on does not delay a safety off
    assert ha.sw == "off" and h.last["reason"] == "hard_cap" and ha.notes
    ha.temp = 66.5                 # would call for heat, but still >= setpoint? no: 66.5 < 68 clears the latch
    h.cap_latched = True
    ha.temp = 70
    run(h, c, 600)
    assert ha.sw == "off" and h.last["reason"] == "hard_cap"
    ha.temp = 66
    run(h, c, 30)
    assert ha.sw == "on"


def test_hard_cap_applies_with_mode_off_and_external_on():
    h, ha, c = mk(temp=85)
    h.set_mode("off")
    ha.sw = "on"
    h.tick()
    assert ha.sw == "off"


# ---------- runtime limits ----------
def test_two_hour_max_run_then_20_min_cooldown():
    h, ha, c = mk(temp=60)
    h.tick()
    t_on = c.t
    rising = iter(range(10_000))

    def warm():  # rises slowly so no-rise never trips, never reaches setpoint
        ha.temp = min(66.9, 60 + next(rising) * 0.02)
    for _ in range(int(2 * 3600 / 30) + 1):
        c.t += 30
        warm()
        h.tick()
        if ha.sw == "off":
            break
    assert c.t - t_on >= 2 * 3600 and h.last["reason"] == "cooldown"
    run(h, c, 19 * 60)
    assert ha.sw == "off"
    run(h, c, 90)
    assert ha.sw == "on"


def test_over_8h_in_24h_lockout_notify_and_reset():
    h, ha, c = mk(temp=60)
    h.runs = [(c.t - 9 * 3600, c.t - 60)]  # fake history: 9 h of running already today
    h.off_since = c.t - 3600
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "lockout"
    assert any("8 h" in n for n in ha.notes)
    run(h, c, 3600)
    assert ha.sw == "off"          # stays locked
    h.runs = []
    h.set_mode("off")
    h.set_mode("heat")
    run(h, c, 30)
    assert ha.sw == "on"


def test_runtime_window_prunes_after_24h():
    h, _, c = mk()
    h.runs = [(c.t - 30 * 3600, c.t - 25 * 3600), (c.t - 3600, c.t - 1800)]
    assert h._on_seconds_24h(c.t) == 1800


# ---------- sensor ----------
@pytest.mark.parametrize("bad", ["unavailable", "unknown", "garbage", "nan"])
def test_sensor_bad_state_off_immediately_with_notify(bad):
    h, ha, c = mk(temp=60)
    h.tick()
    assert ha.sw == "on"
    ha.temp = bad
    run(h, c, 30)
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable" and len(ha.notes) == 1
    run(h, c, 300)
    assert len(ha.notes) == 1      # notify once per episode


def test_sensor_stale_default_75_min():
    """0.8.2: default cutoff is 75 min (Zooz ZSE11 reports hourly or on a 1 F change)."""
    h, ha, c = mk(temp=60)
    h.tick()
    ha.seen = c.t
    run(h, c, 74 * 60)
    assert h.last["reason"] != "sensor_unavailable"
    run(h, c, 60)
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


def test_sensor_stale_minutes_option():
    h, ha, c = mk(temp=60, stale_minutes=20)
    h.tick()
    ha.seen = c.t
    run(h, c, 19 * 60)
    assert ha.sw == "on"
    run(h, c, 60)
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


@pytest.mark.parametrize("raw,want", [(None, 75), (5, 15), (999, 120), (180, 120), ("x", 75), (90, 90)])
def test_stale_minutes_clamped(raw, want):
    assert H.config({"spare_bathroom": {"stale_minutes": raw}})["stale_minutes"] == want


LS = "sensor.fake_last_seen_555"


def _with_ls(ha, value_fn):
    base = ha.get_states
    ha.get_states = lambda: base() + [{"entity_id": LS, "state": value_fn()}]


def test_future_last_seen_cannot_freshen_old_reading():
    h, ha, c = mk(temp=60, last_seen_sensor=LS)
    h.tick()
    assert ha.sw == "on"
    ha.seen = c.t
    _with_ls(ha, lambda: "2036-01-01T00:00:00+00:00")
    run(h, c, 500 * 60, step=300)
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


def test_fresh_last_seen_with_frozen_temp_is_stale():
    h, ha, c = mk(temp=60, last_seen_sensor=LS)
    h.tick()
    ha.seen = c.t
    _with_ls(ha, lambda: iso(c.t - 30))
    run(h, c, 500 * 60, step=300)
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


def test_old_last_seen_tightens():
    h, ha, c = mk(temp=60, last_seen_sensor=LS)
    _with_ls(ha, lambda: iso(c.t - 200 * 60))  # temp entity fresh, node not seen in 200 min
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


def test_naive_last_seen_ignored_temp_decides():
    h, ha, c = mk(temp=60, last_seen_sensor=LS)
    _with_ls(ha, lambda: "2036-01-01T00:00:00")  # naive -> ignored
    h.tick()
    assert ha.sw == "on"                          # fresh temp entity still rules
    ha.seen = c.t
    run(h, c, 500 * 60, step=300)
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


def test_naive_or_future_temp_timestamp_is_stale():
    h, ha, c = mk(temp=60)
    base = ha.get_states
    naive = "2026-11-03T09:00:00"
    ha.get_states = lambda: [dict(x, last_reported=naive) if x["entity_id"] == TS else x for x in base()]
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"
    ha.get_states = lambda: [dict(x, last_reported=iso(c.t + 3600)) if x["entity_id"] == TS else x for x in base()]
    h.tick()
    assert ha.sw == "off" and h.last["reason"] == "sensor_unavailable"


def test_last_seen_sensor_bad_id_ignored():
    assert H.config({"spare_bathroom": {"last_seen_sensor": "switch.nope"}})["last_seen_sensor"] == ""


def test_sensor_missing_entirely():
    h, ha, c = mk(temp=60)
    ha.get_states = lambda: [{"entity_id": SW, "state": "on"}]
    h.commanded = True
    h.tick()
    assert ha.sw == "off"


# ---------- no rise ----------
def test_no_1F_rise_in_45_min_off_and_latched():
    h, ha, c = mk(temp=60)
    h.tick()
    ha.temp = 60.9
    run(h, c, 44 * 60)
    assert ha.sw == "on"
    run(h, c, 60)
    assert ha.sw == "off" and h.last["reason"] == "no_rise" and any("rise" in n for n in ha.notes)
    run(h, c, 3600)
    assert ha.sw == "off"
    h.set_mode("off")
    h.set_mode("heat")
    run(h, c, 30)
    assert ha.sw == "on"


def test_rise_1F_then_stick_off_45_min_after_last_rise():
    """SA M2: HA reports a repeated identical value as fresh; one rise then a stuck reading must still trip."""
    h, ha, c = mk(temp=60)
    h.tick()
    run(h, c, 20 * 60)
    ha.temp = 61                       # one 1 F rise at ~20 min, then stuck
    run(h, c, 30)
    rise_t = c.t
    run(h, c, 44 * 60)
    assert ha.sw == "on"
    run(h, c, 60)
    assert c.t - rise_t <= 45 * 60 + 30
    assert ha.sw == "off" and h.last["reason"] == "no_rise" and h.latched == "no_rise"
    assert any("rise" in n for n in ha.notes)


def test_steady_rises_stay_on_until_2h_max():
    h, ha, c = mk(temp=55)
    h.st["setpoint"] = 75.0
    h.tick()
    t0 = c.t
    for _ in range(2):                 # +1 F every 40 min, never at setpoint
        run(h, c, 40 * 60)
        ha.temp += 1
    run(h, c, 30 * 60)                 # 110 min: well past 45 min from start, still progressing
    assert ha.sw == "on" and h.last["reason"] != "no_rise"
    run(h, c, t0 + 2 * 3600 + 60 - c.t)
    assert ha.sw == "off" and h.latched is None and h.last["reason"] != "no_rise"


def test_reaching_setpoint_is_normal_off():
    h, ha, c = mk(temp=66)
    h.tick()
    assert ha.sw == "on"
    run(h, c, 20 * 60)
    ha.temp = 68.6                     # setpoint 68 + 0.5 -> normal off
    run(h, c, 30)
    assert ha.sw == "off" and h.latched is None and h.last["reason"] != "no_rise"


# ---------- plug / relay ----------
def test_plug_on_while_should_be_off_forced_off_and_notify():
    h, ha, c = mk(temp=70)
    h.tick()
    ha.sw = "on"                   # someone pressed the plug button
    run(h, c, 30)
    assert ha.sw == "off" and any("should be off" in n for n in ha.notes)


def test_power_draw_while_off_is_relay_fault():
    h, ha, c = mk(temp=70, power=0)
    run(h, c, 120)
    ha.power = 1400
    run(h, c, 30)
    assert h.latched == "relay_fault" and any("Relay fault" in n for n in ha.notes)
    ha.temp = 50
    run(h, c, 600)
    assert ha.sw == "off"


def test_power_lag_right_after_off_is_not_a_fault():
    h, ha, c = mk(temp=60, power=0)
    h.tick()
    ha.power = 1400
    ha.temp = 70
    run(h, c, 300)
    assert ha.sw == "off"
    run(h, c, 30)                  # meter still shows draw 30 s after off
    assert h.latched is None


# ---------- door ----------
def test_door_open_pauses_and_close_resumes():
    h, ha, c = mk(temp=60, door="off")
    h.tick()
    assert ha.sw == "on"
    ha.door = "on"
    run(h, c, 30)
    assert ha.sw == "off" and h.last["reason"] == "door_open"
    ha.door = "off"
    run(h, c, 300)
    assert ha.sw == "on"


def test_door_unavailable_counts_as_open():
    h, ha, c = mk(temp=60, door="unavailable")
    h.tick()
    assert ha.sw == "off"


# ---------- startup / shutdown / exceptions ----------
def test_safe_off_startup_shutdown():
    h, ha, _ = mk(temp=60)
    ha.sw = "on"
    h.safe_off("startup")
    assert ha.sw == "off" and h.last["reason"] == "startup"


def test_exception_turns_plug_off():
    h, ha, c = mk(temp=60)
    h.tick()
    assert ha.sw == "on"
    h._decide = lambda *a: 1 / 0
    run(h, c, 30)
    assert ha.sw == "off" and h.last["reason"] == "exception"


def test_ha_read_failure_turns_plug_off():
    h, ha, c = mk(temp=60)
    h.tick()
    ha.fail_states = True
    run(h, c, 30)
    assert ha.sw == "off"


def test_publish_mirror_and_heartbeat():
    h, ha, c = mk(temp=60)
    h.tick()
    st, a = ha.pub[-1]
    assert st == "heat" and a["hvac_modes"] == ["heat", "off"] and a["temperature"] == 68
    assert a["hvac_action"] == "heating" and a["heartbeat"] == c.t


def test_never_touches_climate_entities():
    h, ha, c = mk(temp=60)
    run(h, c, 3 * 3600)
    assert all(isinstance(x, bool) for x in ha.cmds)  # only switch on/off went out


def test_persist_mode_and_setpoint(tmp_path):
    p = str(tmp_path / "h.json")
    c = Clock()
    o = {"spare_bathroom": {"heater_switch": SW, "temp_sensor": TS}}
    h = H.SpareHeater(o, FakeHA(c), p, clock=c)
    h.set_setpoint(72)
    h.set_mode("off")
    h2 = H.SpareHeater(o, FakeHA(c), p, clock=c)
    assert h2.st == {"mode": "off", "setpoint": 72}


# ---------- kitten room: motion is display only, low-temp alerts ----------
MO = "binary_sensor.fake_motion_555"


def with_motion(h, ha, state="on"):
    h.cfg["motion_sensor"] = MO
    base = ha.get_states
    ha.get_states = lambda: base() + [{"entity_id": MO, "state": state}]


def test_motion_never_changes_heating_or_safety():
    for motion in ("on", "off", "unavailable"):
        h, ha, c = mk(temp=67.5)
        with_motion(h, ha, motion)
        run(h, c, 600)
        assert ha.sw == "off" and h.st["setpoint"] == 68       # 67.5 is inside the band: no boost
        ha.temp = 80
        run(h, c, 30)
        assert h.last["reason"] == "hard_cap"
    h, ha, c = mk(temp=60)
    with_motion(h, ha, "on")
    h.runs = [(c.t - 9 * 3600, c.t - 60)]
    h.off_since = c.t - 3600
    run(h, c, 30)
    assert h.last["reason"] == "lockout" and h.last_motion == c.t


def test_no_presence_dependency():
    h, ha, c = mk(temp=60)
    h.opts["presence_entities"] = ["person.fake_a"]   # ignored: kitten room holds 24/7
    h.tick()
    assert ha.sw == "on"


def test_low_warn_after_10_min_once():
    h, ha, c = mk(temp=61, live=False)   # dry run: plug can't help, alert still fires
    run(h, c, 9 * 60)
    assert not any("below 62" in n for n in ha.notes)
    run(h, c, 90)
    warns = [n for n in ha.notes if "below 62" in n]
    assert len(warns) == 1 and "dry run" in warns[0]
    run(h, c, 3600)
    assert len([n for n in ha.notes if "below 62" in n]) == 1


def test_low_crit_repeats_every_30_min_until_recovered_with_cause():
    h, ha, c = mk(temp=60)
    h.runs = [(c.t - 9 * 3600, c.t - 60)]
    h.off_since = c.t - 3600
    ha.temp = 57
    h.tick()
    crit = lambda: [n for n in ha.notes if n.startswith("CRITICAL")]  # noqa: E731
    assert len(crit()) == 1 and "lockout" in crit()[0]
    run(h, c, 29 * 60)
    assert len(crit()) == 1
    run(h, c, 60)
    assert len(crit()) == 2
    ha.temp = 63                        # recovered
    run(h, c, 60)
    ha.temp = 57
    run(h, c, 30)
    assert len(crit()) == 3            # new episode alerts at once


def test_low_alert_thresholds_configurable():
    h, ha, c = mk(temp=64, low_warn_f=65, low_warn_min=1, low_crit_f=64.5)
    run(h, c, 90)
    assert any("below 65" in n for n in ha.notes) and any(n.startswith("CRITICAL") for n in ha.notes)


def test_every_safety_item_alerts():
    h, ha, c = mk(temp=60, door="on")
    h.tick()
    assert any("Door" in n for n in ha.notes)
    h, ha, c = mk(temp=60)
    h.tick()
    h.on_since = c.t - 2 * 3600
    h.rise_ref = (c.t, 50)
    run(h, c, 30)
    assert h.last["reason"] == "cooldown" and any("cooldown" in n for n in ha.notes)
