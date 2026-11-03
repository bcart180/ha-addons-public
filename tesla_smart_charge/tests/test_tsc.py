import os
import sys
from datetime import datetime, timedelta

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
import core  # noqa: E402
from engine import Engine  # noqa: E402

CFG = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
SCHED = [{"days": "mon-fri", "start": "07:00", "end": "11:00"},
         {"days": "mon-fri", "start": "17:00", "end": "21:00"}]


class FakeHA:
    def __init__(self, states):
        self.states = dict(states)
        self.calls = []
        self.notes = []
        self.ble_works = True

    def get_states(self):
        return {k: {"entity_id": k, "state": v} for k, v in self.states.items()}

    def call(self, domain, service, data):
        eid = data["entity_id"]
        self.calls.append((domain, service, eid, data.get("value")))
        if eid.startswith("switch.ble") or eid.startswith("number.ble"):
            if not self.ble_works:
                return
        if domain == "switch":
            self.states["switch.car_charge"] = "on" if service == "turn_on" else "off"
        elif domain == "number" and "current" in eid:
            self.states["number.car_charge_current"] = str(data["value"])
        elif domain == "number" and "limit" in eid:
            self.states["number.car_charge_limit"] = str(data["value"])
        elif domain == "input_boolean":
            self.states[eid] = "off"

    def notify(self, t, m):
        self.notes.append(m)

    def set_status(self, s, a):
        pass


OPTS = {"dry_run": False, "confirm_s": 5, "charge_switch": "switch.car_charge",
        "charge_current": "number.car_charge_current", "charge_limit": "number.car_charge_limit",
        "battery_level": "sensor.car_battery_level", "plugged_in": "binary_sensor.car_charge_cable",
        "peak_sensor": "binary_sensor.utility_peak", "charge_now_entity": "input_boolean.charge_now",
        "pause_entity": "input_boolean.charge_pause", "trip_entity": "input_boolean.trip_mode",
        "trip_target_soc": 100, "trip_departure_time": "05:00",
        "battery_kwh": 100, "target_soc": 80, "departure_time": "06:00", "finish_buffer_min": 30,
        "min_amps": 5, "max_amps": 40, "fallback_voltage": 240, "check_interval_min": 10,
        "notify_service": "notify.example_phone", "notify_not_charging_min": 20}
BASE = {"switch.car_charge": "off", "number.car_charge_current": "16", "number.car_charge_limit": "80",
        "sensor.car_battery_level": "50", "binary_sensor.car_charge_cable": "on",
        "binary_sensor.utility_peak": "off", "input_boolean.charge_now": "off",
        "input_boolean.charge_pause": "off", "input_boolean.trip_mode": "off"}


def mk(t, opts=None, **st):
    ha = FakeHA(dict(BASE, **st))
    clock = {"t": t}
    e = Engine(dict(OPTS, **(opts or {})), ha, clock=lambda: clock["t"], sleep=lambda s: None)
    return e, ha, clock


NIGHT = datetime(2026, 11, 2, 21, 0)  # Monday


def test_lowest_amps_to_target():
    a, kwh = core.required_amps(50, 80, 100, 0.9, 240, 8.5)
    assert round(kwh, 2) == 33.33 and a == 17  # ceil(33333/(240*8.5)) = 17


def test_plan_starts_with_lowest_amps_and_clamps():
    e, ha, _ = mk(NIGHT)
    p = e.tick()
    assert p["action"] == "start" and p["amps"] == 17
    assert ("number", "set_value", "number.car_charge_current", 17) in ha.calls
    assert ha.states["switch.car_charge"] == "on"


def test_only_commands_on_1a_change():
    e, ha, _ = mk(NIGHT, **{"number.car_charge_current": "17", "switch.car_charge": "on"})
    e.tick()
    assert not [c for c in ha.calls if c[0] == "number" and "current" in c[2]]


def test_dry_run_never_turns_on():
    e, ha, _ = mk(NIGHT, opts={"dry_run": True})
    p = e.tick()
    assert p["action"] == "start" and ha.calls == []


def test_dry_run_safety_off_still_fires_in_peak():
    e, ha, _ = mk(NIGHT, opts={"dry_run": True}, **{"binary_sensor.utility_peak": "on", "switch.car_charge": "on"})
    p = e.tick()
    assert p["action"] == "stop" and ("switch", "turn_off", "switch.car_charge", None) in ha.calls
    assert not [c for c in ha.calls if c[1] == "turn_on"]


def test_never_charges_in_peak_unless_charge_now():
    e, ha, _ = mk(NIGHT, **{"binary_sensor.utility_peak": "on"})
    assert e.tick()["action"] == "stop"
    e2, ha2, _ = mk(NIGHT, **{"binary_sensor.utility_peak": "on", "input_boolean.charge_now": "on"})
    p = e2.tick()
    assert p["action"] == "start" and p["amps"] == 40 and ha2.states["switch.car_charge"] == "on"


def test_unknown_peak_sensor_fails_safe():
    e, ha, _ = mk(NIGHT, **{"binary_sensor.utility_peak": "unavailable", "switch.car_charge": "on"})
    assert e.tick()["action"] == "stop"


def test_nan_soc_guard():
    for bad in ("unavailable", "unknown", "nan", "inf", ""):
        e, ha, _ = mk(NIGHT, **{"sensor.car_battery_level": bad})
        p = e.tick()
        assert p["action"] in ("hold", "stop") and not [c for c in ha.calls if c[1] == "turn_on"]


def test_peak_off_edge_starts_immediately():
    e, ha, clock = mk(datetime(2026, 11, 2, 20, 59), **{"binary_sensor.utility_peak": "on"})
    e.tick()
    clock["t"] = datetime(2026, 11, 2, 21, 0, 1)
    ha.states["binary_sensor.utility_peak"] = "off"
    p = e.tick()  # well inside the 10-min interval: edge triggers the plan
    assert p["action"] == "start" and ha.states["switch.car_charge"] == "on"


def test_charge_now_auto_clears_on_complete_and_unplug():
    e, ha, _ = mk(NIGHT, **{"input_boolean.charge_now": "on", "sensor.car_battery_level": "80"})
    e.tick()
    assert ha.states["input_boolean.charge_now"] == "off"
    e, ha, _ = mk(NIGHT, **{"input_boolean.charge_now": "on", "binary_sensor.car_charge_cable": "off"})
    e.tick()
    assert ha.states["input_boolean.charge_now"] == "off"


def test_pause_holds_until_next_window():
    e, ha, clock = mk(NIGHT, **{"input_boolean.charge_pause": "on", "switch.car_charge": "on"})
    assert e.tick()["action"] == "stop" and ha.states["switch.car_charge"] == "off"
    clock["t"] = NIGHT + timedelta(minutes=10)
    assert e.tick()["action"] == "stop"  # the next check does NOT restart it
    ha.states["binary_sensor.utility_peak"] = "on"
    clock["t"] = NIGHT + timedelta(hours=10)
    e.tick()
    ha.states["binary_sensor.utility_peak"] = "off"
    clock["t"] = NIGHT + timedelta(hours=20)
    p = e.tick()
    assert ha.states["input_boolean.charge_pause"] == "off" and p["action"] == "start"


def test_trip_mode_overrides_target_and_departure():
    e, ha, _ = mk(NIGHT, **{"input_boolean.trip_mode": "on"})
    s = e.inputs(ha.get_states(), NIGHT)
    assert s["target"] == 100 and s["departure"] == datetime(2026, 11, 3, 5, 0)


def test_ble_first_then_cloud_fallback():
    o = {"ble_charge_switch": "switch.ble_charger", "ble_charge_current": "number.ble_charging_amps"}
    e, ha, _ = mk(NIGHT, opts=o)
    e.tick()
    assert [c[2] for c in ha.calls if c[0] == "switch"] == ["switch.ble_charger"]
    e, ha, _ = mk(NIGHT, opts=o)
    ha.ble_works = False
    e.tick()
    assert [c[2] for c in ha.calls if c[0] == "switch"] == ["switch.ble_charger", "switch.car_charge"]


def test_notify_once_if_not_charging():
    e, ha, clock = mk(NIGHT)
    ha.call = lambda *a, **k: None  # car ignores every command
    e.tick()
    clock["t"] = NIGHT + timedelta(minutes=25)
    e.tick()
    clock["t"] = NIGHT + timedelta(minutes=35)
    e.tick()
    assert len(ha.notes) == 1


def test_voltage_sane_range():
    assert core.clamp_voltage("120", 240) == 240 and core.clamp_voltage("nan", 240) == 240
    assert core.clamp_voltage("208", 240) == 208


def test_schedule_and_offpeak_hours():
    assert core.in_schedule(datetime(2026, 11, 2, 18, 0), SCHED)
    assert not core.in_schedule(datetime(2026, 11, 7, 18, 0), SCHED)  # Saturday
    assert core.in_schedule(datetime(2026, 11, 3, 1, 0), [{"days": "mon", "start": "23:00", "end": "02:00"}])
    h = core.offpeak_hours(datetime(2026, 11, 2, 16, 0), datetime(2026, 11, 2, 22, 0), SCHED)
    assert abs(h - 2.0) < 1e-6


def test_past_finish_uses_max():
    p = core.plan(dict(now=datetime(2026, 11, 3, 5, 45), soc=70, target=80, plugged=True, home=None, peak=False,
                       charge_now=False, pause=False, departure=datetime(2026, 11, 3, 6, 0), finish_buffer_min=30,
                       battery_kwh=75, efficiency=0.9, volts=240, min_amps=5, max_amps=32, schedule=[]))
    assert p["action"] == "start" and p["amps"] == 32 and p["behind"]


def test_public_defaults_safe_and_generic():
    cfg = yaml.safe_load(open(CFG))
    o = cfg["options"]
    assert o["dry_run"] is True and o["check_interval_min"] == 10 and o["charge_efficiency"] == 0.9
    for k in ("charge_switch", "charge_current", "battery_level", "plugged_in"):
        assert o[k] == ""


def test_unconfigured_is_inert():
    ha = FakeHA({})
    e = Engine({"dry_run": False}, ha, clock=lambda: NIGHT, sleep=lambda s: None)
    assert e.tick() is None and ha.calls == []


def test_m1_max_wins_over_min():
    e, ha, _ = mk(NIGHT, opts={"min_amps": 30, "max_amps": 20})
    assert e.o["min_amps"] == 20
    p = e.tick()
    assert p["amps"] <= 20
    p2 = core.plan(dict(now=NIGHT, soc=79, target=80, plugged=True, home=None, peak=False, charge_now=False,
                        pause=False, departure=NIGHT + timedelta(hours=9), finish_buffer_min=30, battery_kwh=75,
                        efficiency=0.9, volts=240, min_amps=30, max_amps=20, schedule=[]))
    assert p2["amps"] == 20


def test_l2_hard_ceiling_48():
    e, ha, _ = mk(NIGHT, opts={"max_amps": 80, "min_amps": 5})
    assert e.o["max_amps"] == 48
    cfg = yaml.safe_load(open(CFG))
    assert cfg["schema"]["max_amps"] == "int(1,48)" and cfg["options"]["dry_run_safety_off"] is True


class StampHA(FakeHA):
    def __init__(self, states, stamps):
        super().__init__(states)
        self.stamps = stamps

    def get_states(self):
        out = super().get_states()
        for k, v in self.stamps.items():
            out[k]["last_reported"] = v
        return out


def _stale_engine(**st):
    now = datetime(2026, 11, 2, 21, 0).astimezone()
    old = (now - timedelta(minutes=45)).isoformat()
    ha = StampHA(dict(BASE, **st), {"sensor.car_battery_level": old})
    o = dict(OPTS, online_sensor="binary_sensor.car_status", wake_button="button.car_wake")
    ha.states["binary_sensor.car_status"] = "off"
    e = Engine(o, ha, clock=lambda: now, sleep=lambda s: None)
    return e, ha


def test_l1_stale_soc_never_starts_but_wakes():
    e, ha = _stale_engine()
    p = e.tick()
    assert p["action"] == "hold" and p["reason"] == "stale_state"
    assert not [c for c in ha.calls if c[0] in ("switch", "number")]
    assert ("button", "press", "button.car_wake", None) in ha.calls


def test_l1_stale_in_peak_still_stops():
    e, ha = _stale_engine(**{"binary_sensor.utility_peak": "on", "switch.car_charge": "on"})
    assert e.tick()["action"] == "stop" and ha.states["switch.car_charge"] == "off"


def test_l1_fresh_stamp_plans_normally():
    now = datetime(2026, 11, 2, 21, 0).astimezone()
    ha = StampHA(dict(BASE), {"sensor.car_battery_level": (now - timedelta(minutes=5)).isoformat()})
    e = Engine(dict(OPTS), ha, clock=lambda: now, sleep=lambda s: None)
    assert e.tick()["action"] == "start"


def _l3(plug_stale=False, opts=None, now=None, **st):
    now = now or datetime(2026, 11, 2, 21, 0).astimezone()
    old = (now - timedelta(minutes=45)).isoformat()
    stamps = {"sensor.car_battery_level": old}
    if plug_stale:
        stamps["binary_sensor.car_charge_cable"] = old
    base = dict(BASE, **{"binary_sensor.car_status": "off"})
    ha = StampHA(dict(base, **st), stamps)
    clock = {"t": now}
    o = dict(OPTS, online_sensor="binary_sensor.car_status", wake_button="button.car_wake", **(opts or {}))
    e = Engine(o, ha, clock=lambda: clock["t"], sleep=lambda s: None, mono=lambda: clock["t"].timestamp())
    return e, ha, clock


def _wakes(ha):
    return len([c for c in ha.calls if c[0] == "button"])


def test_l3_gate_plug_must_be_fresh_plugged():
    e, ha, _ = _l3(plug_stale=True)
    e.tick()
    assert _wakes(ha) == 0
    e, ha, _ = _l3(**{"binary_sensor.car_charge_cable": "off"})
    e.tick()
    assert _wakes(ha) == 0


def test_l3_gate_home():
    e, ha, _ = _l3(opts={"home_entity": "device_tracker.car_location"},
                   **{"device_tracker.car_location": "not_home"})
    e.tick()
    assert _wakes(ha) == 0
    e, ha, _ = _l3(opts={"home_entity": "device_tracker.car_location"}, **{"device_tracker.car_location": "home"})
    e.tick()
    assert _wakes(ha) == 1


def test_l3_gate_peak_unless_charge_now():
    e, ha, _ = _l3(**{"binary_sensor.utility_peak": "on"})
    e.tick()
    assert _wakes(ha) == 0
    e, ha, _ = _l3(**{"binary_sensor.utility_peak": "on", "input_boolean.charge_now": "on"})
    e.tick()
    assert _wakes(ha) == 1


def test_l3_gate_soc_below_target():
    e, ha, _ = _l3(**{"sensor.car_battery_level": "85"})
    e.tick()
    assert _wakes(ha) == 0


def test_l3_backoff_and_cap():
    e, ha, clock = _l3(opts={"wake_max_per_window": 3})
    start = clock["t"]
    times = []
    for m in range(0, 400, 10):
        clock["t"] = start + timedelta(minutes=m)
        before = _wakes(ha)
        e.last_plan = None
        e.tick()
        if _wakes(ha) > before:
            times.append(m)
    assert times == [0, 10, 30]  # 10 then 20 min apart, capped at 3


def test_l3_backoff_resets_on_plug_change():
    e, ha, clock = _l3()
    e.tick()
    assert e.wake_count == 1
    ha.states["binary_sensor.car_charge_cable"] = "off"
    clock["t"] += timedelta(minutes=1)
    e.tick()
    assert e.wake_count == 1  # unplug alone does not reset
    ha.states["binary_sensor.car_charge_cable"] = "on"
    clock["t"] += timedelta(minutes=1)
    e.tick()
    assert e.wake_count <= 1 and _wakes(ha) == 1  # real off->on resets, but 10-min gap still holds


def test_l3_backoff_sequence_caps_at_120():
    e, _, _ = _l3(opts={"wake_max_per_window": 20})
    s = {"now": datetime(2026, 11, 2, 21, 0), "last_plugged": True, "home": None, "peak": False,
         "charge_now": False, "last_soc": 50, "target": 80}
    gaps, t = [], s["now"]
    for _ in range(6):
        s["now"] = t
        assert e.stale_wake_allowed(s) and e._wake_budget(t)
        gaps.append(int((e.next_wake_at - t).total_seconds() // 60))
        t = e.next_wake_at
    assert gaps == [10, 20, 40, 80, 120, 120]


def _run(e, ha, clock, minutes, flap=None):
    start = clock["t"]
    for m in range(minutes):
        clock["t"] = start + timedelta(minutes=m)
        if flap:
            flap(m, ha)
        e.tick()


def test_l4_plug_flap_unavailable_does_not_reset_cap():
    e, ha, clock = _l3(opts={"wake_max_per_window": 3})

    def flap(m, ha):
        ha.states["binary_sensor.car_charge_cable"] = "unavailable" if m % 2 else "on"
    _run(e, ha, clock, 600, flap)
    assert _wakes(ha) == 3


def test_l4_peak_flap_unavailable_does_not_open_window():
    e, ha, clock = _l3(opts={"wake_max_per_window": 2})

    def flap(m, ha):
        ha.states["binary_sensor.utility_peak"] = "unavailable" if m % 2 else "off"
    _run(e, ha, clock, 600, flap)
    assert _wakes(ha) == 2


def test_l4_fresh_data_does_not_reset_cap():
    e, ha, clock = _l3(opts={"wake_max_per_window": 2})
    _run(e, ha, clock, 200)
    assert _wakes(ha) == 2
    ha.stamps = {}  # data becomes fresh
    _run(e, ha, clock, 5)
    assert e.wake_count == 2
    ha.stamps = {"sensor.car_battery_level": (clock["t"] - timedelta(minutes=45)).isoformat()}
    _run(e, ha, clock, 300)
    assert _wakes(ha) == 2


def test_l4_new_window_resets_cap_but_min_gap_holds():
    e, ha, clock = _l3(opts={"wake_max_per_window": 1})
    _run(e, ha, clock, 30)
    assert _wakes(ha) == 1
    ha.states["binary_sensor.utility_peak"] = "on"
    clock["t"] += timedelta(minutes=1)
    e.tick()
    ha.states["binary_sensor.utility_peak"] = "off"
    clock["t"] += timedelta(minutes=1)
    e.tick()
    assert _wakes(ha) == 2


def test_l4_hard_min_gap_between_any_wakes():
    e, ha, clock = _l3(opts={"wake_max_per_window": 20})
    e.last_wake_mono = clock["t"].timestamp()
    e.tick()
    assert _wakes(ha) == 0  # backoff would allow, min gap blocks
    clock["t"] += timedelta(minutes=10)
    e.last_plan = None
    e.tick()
    assert _wakes(ha) == 1
