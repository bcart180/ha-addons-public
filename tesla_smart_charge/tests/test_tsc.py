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
        self.attrs = {}

    def get_states(self):
        return {k: {"entity_id": k, "state": v, "attributes": dict(self.attrs.get(k, {}))}
                for k, v in self.states.items()}

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
    e = Engine(dict(OPTS, **(opts or {})), ha, clock=lambda: clock["t"], sleep=lambda s: None,
               mono=lambda: clock["t"].timestamp())
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
    gaps, t = [], 0.0
    e.mono = lambda: t
    for _ in range(6):
        assert e.stale_wake_allowed(s) and e._wake_budget(None)
        gaps.append(int((e.wake_bo.next_at - t) // 60))
        t = e.wake_bo.next_at
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


def _on_calls(ha):
    return len([c for c in ha.calls if c[:2] == ("switch", "turn_on")])


def test_charge_on_retry_backoff_and_cap():
    e, ha, clock = mk(NIGHT)
    ha.call = lambda d, sv, data: ha.calls.append((d, sv, data["entity_id"], data.get("value")))  # car ignores
    start, sent = clock["t"], []
    for m in range(0, 600, 10):
        clock["t"] = start + timedelta(minutes=m)
        before = _on_calls(ha)
        e.tick()
        if _on_calls(ha) > before:
            sent.append(m)
    assert sent == [0, 10, 30, 70, 150]  # 10/20/40/80 apart, cap 5


def test_charge_on_backoff_resets_on_new_window():
    e, ha, clock = mk(NIGHT, opts={"charge_on_max_per_window": 1})
    ha.call = lambda d, sv, data: ha.calls.append((d, sv, data["entity_id"], data.get("value")))
    e.tick()
    clock["t"] += timedelta(minutes=30)
    e.tick()
    assert _on_calls(ha) == 1
    ha.states["binary_sensor.utility_peak"] = "on"
    clock["t"] += timedelta(minutes=1)
    e.tick()
    ha.states["binary_sensor.utility_peak"] = "off"
    clock["t"] += timedelta(minutes=1)
    e.tick()
    assert _on_calls(ha) == 2


def test_unknown_charging_sensor_still_stops():
    for bad in ("unavailable", "unknown"):
        e, ha, _ = mk(NIGHT, opts={"charging_sensor": "binary_sensor.car_charging"},
                      **{"binary_sensor.car_charging": bad, "binary_sensor.utility_peak": "on"})
        e.tick()
        assert ("switch", "turn_off", "switch.car_charge", None) in ha.calls
        e, ha, _ = mk(NIGHT, opts={"charging_sensor": "binary_sensor.car_charging"},
                      **{"binary_sensor.car_charging": bad, "input_boolean.charge_pause": "on"})
        e.tick()
        assert ("switch", "turn_off", "switch.car_charge", None) in ha.calls


def test_la_live_without_limit_refuses_start():
    o = dict(OPTS)
    o.pop("charge_limit")
    ha = FakeHA(dict(BASE))
    e = Engine(o, ha, clock=lambda: NIGHT, sleep=lambda s: None)
    assert e.tick()["action"] == "start"
    assert not [c for c in ha.calls if c[1] in ("turn_on", "set_value")]
    ha.states["binary_sensor.utility_peak"] = "on"
    ha.states["switch.car_charge"] = "on"
    e.tick()
    assert ("switch", "turn_off", "switch.car_charge", None) in ha.calls


def test_lb_wall_clock_jump_back_does_not_suppress_plans():
    t = {"wall": NIGHT, "mono": 1000.0}
    ha = FakeHA(dict(BASE))
    e = Engine(dict(OPTS), ha, clock=lambda: t["wall"], sleep=lambda s: None, mono=lambda: t["mono"])
    assert e.tick() is not None
    t["wall"] = NIGHT - timedelta(hours=2)  # wall jumps back
    t["mono"] += 600
    assert e.tick() is not None


def test_lc_future_stamp_is_stale():
    now = datetime(2026, 11, 2, 21, 0).astimezone()
    ha = StampHA(dict(BASE), {"sensor.car_battery_level": (now + timedelta(minutes=5)).isoformat()})
    e = Engine(dict(OPTS), ha, clock=lambda: now, sleep=lambda s: None)
    assert e.tick()["reason"] == "stale_state"
    ha.stamps = {"sensor.car_battery_level": (now + timedelta(seconds=30)).isoformat()}
    e.last_plan = None
    assert e.tick()["action"] == "start"


def test_le_no_wake_button_keeps_budget():
    e, ha, _ = _l3()
    e.o["wake_button"] = ""
    e.tick()
    assert e.wake_count == 0


def test_020_taper_adds_time_above_taper_soc():
    a0, w0 = core.required_amps(50, 100, 100, 0.9, 240, 8)
    a1, w1 = core.required_amps(50, 100, 100, 0.9, 240, 8, taper_soc=90, taper_factor=1.5)
    assert w0 == w1 and a1 > a0
    assert core.taper_equiv_kwh(50, 80, 100, 0.9, 90, 1.5)[1] == core.taper_equiv_kwh(50, 80, 100, 0.9)[1]


def test_020_tou_cost_estimate():
    rates = [{"days": "all", "start": "21:00", "end": "07:00", "price": 0.10},
             {"days": "all", "start": "07:00", "end": "21:00", "price": 0.30}]
    assert core.est_cost(datetime(2026, 11, 2, 22, 0), 4, 40, rates, None) == 4.0
    assert core.est_cost(datetime(2026, 11, 2, 22, 0), 4, 40, [], None) is None
    assert core.est_cost(datetime(2026, 11, 2, 22, 0), 4, 40, [], 0.2) == 8.0


def test_020_next_offpeak():
    assert core.next_offpeak(datetime(2026, 11, 2, 18, 0), SCHED) == datetime(2026, 11, 2, 21, 0)
    assert core.next_offpeak(datetime(2026, 11, 2, 22, 0), SCHED) == datetime(2026, 11, 2, 22, 0)


def test_020_status_attributes_for_card():
    e, ha, _ = mk(NIGHT, opts={"rates": [{"days": "all", "start": "00:00", "end": "23:59", "price": 0.12}]})
    seen = {}
    ha.set_status = lambda s, a: seen.update(a, state=s)
    e.tick()
    for k in ("plan_amps", "kwh_needed", "eta_finish", "est_cost", "mode", "transport", "next_action",
              "last_error", "start_at", "finish_by", "setup_mode", "charge_now_entity", "pause_entity", "trip_entity"):
        assert k in seen, k
    assert seen["mode"] == "smart" and seen["transport"] == "cloud" and seen["setup_mode"] == "cloud-only"
    assert seen["est_cost"] == round(33.33 * 0.12, 2) or abs(seen["est_cost"] - 4.0) < 0.05
    assert seen["eta_finish"] <= seen["finish_by"]


def test_020_status_peak_shows_next_window_start():
    e, ha, _ = mk(datetime(2026, 11, 2, 18, 0), opts={"peak_schedule": SCHED, "peak_sensor": ""})
    seen = {}
    ha.set_status = lambda s, a: seen.update(a)
    e.tick()
    assert seen["next_action"] == "wait for off-peak" and seen["start_at"].startswith("2026-11-02T21:00")


def test_020_ble_transport_and_last_error():
    o = {"ble_charge_switch": "switch.ble_charger", "ble_charge_current": "number.ble_charging_amps"}
    e, ha, _ = mk(NIGHT, opts=o)
    e.tick()
    assert e.transport == "ble" and e.summary(core.plan(e.inputs(ha.get_states(), NIGHT)),
                                              e.inputs(ha.get_states(), NIGHT))["setup_mode"] == "ble+cloud"

    def boom(*a):
        raise RuntimeError("HTTP 500")
    e2, ha2, _ = mk(NIGHT)
    ha2.call = boom
    e2.tick()
    assert "HTTP 500" in e2.last_error


def test_020_self_check_reports_missing():
    e, ha, _ = mk(NIGHT, opts={"voltage_sensor": "sensor.not_there"})
    r = e.self_check(ha.get_states())
    assert r["mode"] == "cloud-only" and any("sensor.not_there" in m for m in r["missing"])
    assert not r["required_empty"]


def test_020_defaults_public_safe():
    cfg = yaml.safe_load(open(CFG))
    assert cfg["version"] == "0.3.1" and cfg["options"]["rates"] == [] and cfg["options"]["taper_soc"] == 90


# ---------------- 0.3.0 ----------------
WC = {"wall_connector_status": "sensor.wc_status", "handle_temp_sensor": "sensor.wc_handle_temp",
      "actual_current_sensor": "sensor.car_charger_current", "departure_time": "05:30", "finish_buffer_min": 30,
      "target_soc": 100, "battery_kwh": 100, "max_amps": 40}


def amp_sets(ha):
    return [v for (d, svc, eid, v) in ha.calls if d == "number" and "current" in eid]


def run_night(e, ha, clock, start, end, events, step_min=10):
    """Advance the clock in check_interval steps, applying `events` {datetime: fn(ha)} as they come due."""
    t = start
    log = []
    while t <= end:
        for when in sorted(list(events)):
            if when <= t:
                events.pop(when)(ha)
        clock["t"] = t
        p = e.tick()
        if p:
            log.append((t, p["action"], p["amps"], p["reason"]))
        # the car follows the commanded current unless derated
        if ha.states.get("switch.car_charge") == "on" and "sensor.car_charger_current" not in ha.frozen:
            ha.states["sensor.car_charger_current"] = ha.states["number.car_charge_current"]
        if ha.states.get("switch.car_charge") == "on":  # simulate energy delivered (100 kWh pack, 240 V, 90%)
            amps = float(ha.states.get("sensor.car_charger_current") or 0)
            soc = float(ha.states["sensor.car_battery_level"]) + amps * 240 * step_min / 60 / 1000 * 0.9
            ha.states["sensor.car_battery_level"] = "%.2f" % min(soc, 100)
        t += timedelta(minutes=step_min)
    return log


def night(opts=None, **st):
    e, ha, clock = mk(datetime(2026, 11, 3, 21, 0), dict(WC, **(opts or {})), **dict(
        {"sensor.car_battery_level": "60", "number.car_charge_limit": "100", "sensor.wc_status": "charging",
         "sensor.wc_handle_temp": "80", "sensor.car_charger_current": "0"}, **st))
    ha.attrs["number.car_charge_current"] = {"min": 0, "max": 48}
    ha.attrs["sensor.wc_handle_temp"] = {"unit_of_measurement": "\u00b0F"}
    ha.frozen = set()
    return e, ha, clock


def test_030_session_departure_rollover_pure():
    lat = datetime(2026, 11, 4, 5, 30)
    assert core.session_departure(lat, lat + timedelta(days=1), datetime(2026, 11, 4, 5, 31)) == lat
    assert core.session_departure(lat, lat + timedelta(days=1), datetime(2026, 11, 4, 12, 0)) == lat + timedelta(days=1)
    other = datetime(2026, 11, 4, 7, 0)
    assert core.session_departure(lat, other, datetime(2026, 11, 4, 1, 0)) == other
    assert core.session_departure(None, other, datetime(2026, 11, 4, 1, 0)) == other


def test_030_replay_last_night_rollover_derate_fault():
    """Replay of the incident: steady ramp-down, handle reads 491 F at 02:33, car's current range drops to 0-6 A,
    the connector faults at 03:04, and the 05:00 deadline passes. Expect: no amp raise toward 40 A, never above
    the 6 A cap, holds on fault, and no 5 A min-amps command at the deadline."""
    e, ha, clock = night()

    def hot(h):
        h.states["sensor.wc_handle_temp"] = "491"
        h.attrs["number.car_charge_current"] = {"min": 0, "max": 6}
        h.states["sensor.car_charger_current"] = "6"
        h.frozen.add("sensor.car_charger_current")

    def fault(h):
        h.states["sensor.wc_status"] = "error"

    log = run_night(e, ha, clock, datetime(2026, 11, 3, 21, 0), datetime(2026, 11, 4, 5, 30),
                    {datetime(2026, 11, 4, 2, 33): hot, datetime(2026, 11, 4, 3, 4): fault})
    before = [a for (t, act, a, r) in log if t < datetime(2026, 11, 4, 2, 33) and act == "start"]
    assert before and max(before) < 40
    sets_after = [v for v in amp_sets(ha)[len(before):]]
    assert 40 not in amp_sets(ha) and all(v <= 6 for v in sets_after)
    after = [(t, act, r) for (t, act, a, r) in log if t >= datetime(2026, 11, 4, 2, 40)]
    assert after and all(act == "stop" and r in ("handle_too_hot", "wall_connector_fault") for (t, act, r) in after)
    assert after[0][2] == "handle_too_hot"
    assert ("switch", "turn_off", "switch.car_charge", None) in ha.calls
    assert ha.notes and "491" in ha.notes[0]
    assert e.status["next_action"]


def test_030_rollover_at_deadline_no_min_amps():
    """No fault sensors: at/after 05:00 (finish-by) the add-on stops adjusting instead of planning for tomorrow."""
    e, ha, clock = night({"wall_connector_status": "", "handle_temp_sensor": "", "actual_current_sensor": ""},
                         **{"sensor.car_battery_level": "40", "sensor.car_charger_current": "8"})
    ha.frozen.add("sensor.car_charger_current")  # car under-delivers, so it is still short at the deadline
    log = run_night(e, ha, clock, datetime(2026, 11, 3, 21, 0), datetime(2026, 11, 4, 6, 0), {})
    n_before = len([1 for (t, act, a, r) in log if t < datetime(2026, 11, 4, 5, 0)])
    after = log[n_before:]
    assert after and all(act == "hold" and r == "past_finish_time" for (t, act, a, r) in after)
    calls_after = [c for c in ha.calls[-len(after) * 3:] if c[0] == "number" and "current" in c[2]]
    assert 5 not in [c[3] for c in calls_after]
    assert e.status["finish_by"].startswith("2026-11-04T05:00")


def test_030_late_plug_in_still_charges():
    e, ha, clock = night({"wall_connector_status": "", "handle_temp_sensor": "", "actual_current_sensor": ""})
    clock["t"] = datetime(2026, 11, 4, 5, 10)
    p = e.tick()
    assert p["action"] == "start" and p["reason"] == "late_plug_in"


def test_030_unplug_clears_session():
    e, ha, clock = night({"wall_connector_status": "", "handle_temp_sensor": ""})
    clock["t"] = datetime(2026, 11, 4, 1, 0)
    e.tick()
    assert e.session_dep is not None
    ha.states["binary_sensor.car_charge_cable"] = "off"
    clock["t"] = datetime(2026, 11, 4, 1, 20)
    e.tick()
    assert e.session_dep is None


def test_030_clamp_to_number_max_rejects_40():
    e, ha, clock = night()
    ha.attrs["number.car_charge_current"] = {"min": 0, "max": 6}
    e.o["charge_now_entity"] = "input_boolean.charge_now"
    ha.states["input_boolean.charge_now"] = "on"
    p = e.tick()
    assert p["amps"] == 6 and 40 not in amp_sets(ha)


def test_030_ramp_up_limited_and_down_immediate():
    s = {"charging": True, "amps_now": "10", "ramp_up_max_a": 4}
    assert core.guard({"action": "start", "amps": 32, "reason": "x"}, s)["amps"] == 14
    assert core.guard({"action": "start", "amps": 6, "reason": "x"}, s)["amps"] == 6
    assert core.guard({"action": "start", "amps": 32, "reason": "x"}, dict(s, charging=False))["amps"] == 32


def test_030_derate_detected_after_two_checks_and_not_fought():
    e, ha, clock = night({"wall_connector_status": "", "handle_temp_sensor": ""})
    ha.states.update({"switch.car_charge": "on", "number.car_charge_current": "18", "sensor.car_charger_current": "6"})
    st = ha.get_states()
    assert e.derated(st, 60, True) is False and e.derated(st, 60, True) is True
    assert e.derated(st, 95, True) is False  # taper zone is not a derate
    assert core.guard({"action": "start", "amps": 24, "reason": "x"},
                      {"derated": True, "amps_now": "18"})["amps"] == 18


def test_030_fault_hold_and_stop_untouched():
    assert core.wc_fault("Error", None, 150) == "wall_connector_fault"
    assert core.wc_fault("charging", 151, 150) == "handle_too_hot"
    assert core.wc_fault("unavailable", None, 150) is None
    assert core.temp_f("70", "\u00b0C") == 158.0
    assert core.guard({"action": "start", "amps": 10, "reason": "x"}, {"fault": "handle_too_hot"})["action"] == "stop"
    stop = {"action": "stop", "amps": None, "reason": "peak"}
    assert core.guard(stop, {"fault": "wall_connector_fault"}) is stop


def test_030_helper_ids_validated():
    e, ha, clock = mk(NIGHT, {"pause_entity": "switch.not_a_helper", "trip_entity": "input_boolean.Bad-Id",
                              "charge_now_entity": "input_boolean.ok_1"})
    assert e.o["pause_entity"] == "" and e.o["trip_entity"] == "" and e.o["charge_now_entity"] == "input_boolean.ok_1"
    sch = yaml.safe_load(open(CFG))["schema"]
    for k in ("enabled_entity", "charge_now_entity", "pause_entity", "trip_entity"):
        assert sch[k] == "match(^input_boolean\\.[a-z0-9_]+$)?"


def test_030_public_scrub():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    bad = ("echo show", "echo_show", "192.168.", "webhook_id")
    for dp, _, fs in os.walk(root):
        if "__pycache__" in dp or os.sep + "tests" in dp[len(root):]:
            continue
        for f in fs:
            txt = open(os.path.join(dp, f), errors="ignore").read().lower()
            for b in bad:
                assert b not in txt, (f, b)


def _fault_engine(opts=None, **st):
    e, ha, clock = night(opts, **dict({"switch.car_charge": "on", "sensor.wc_status": "error"}, **st))
    return e, ha, clock


def _switched_off(ha):
    return any(c[0] == "switch" and c[1] == "turn_off" for c in ha.calls)


def test_031_fault_while_charging_stops():
    e, ha, clock = _fault_engine()
    p = e.tick()
    assert p["action"] == "stop" and p["reason"] == "wall_connector_fault" and _switched_off(ha)
    assert not any(c[1] == "turn_on" for c in ha.calls)


def test_031_hot_handle_stops_in_dry_run():
    e, ha, clock = _fault_engine({"dry_run": True}, **{"sensor.wc_status": "charging", "sensor.wc_handle_temp": "160"})
    p = e.tick()
    assert p["reason"] == "handle_too_hot" and _switched_off(ha)


def test_031_fault_overrides_charge_now():
    e, ha, clock = _fault_engine(**{"input_boolean.charge_now": "on"})
    p = e.tick()
    assert p["action"] == "stop" and _switched_off(ha)


def test_031_fault_past_deadline_stops():
    e, ha, clock = night({}, **{"switch.car_charge": "on", "sensor.car_battery_level": "40"})
    clock["t"] = datetime(2026, 11, 4, 4, 0)
    e.tick()  # session latched
    ha.states["sensor.wc_status"] = "fault"
    clock["t"] = datetime(2026, 11, 4, 5, 10)
    p = e.tick()
    assert p["action"] == "stop" and p["reason"] == "wall_connector_fault" and _switched_off(ha)


def test_031_fault_idle_and_stale_stop():
    e, ha, clock = _fault_engine(**{"input_boolean.charge_pause": "off"})
    e.o["enabled_entity"] = "input_boolean.tsc_enabled"
    ha.states["input_boolean.tsc_enabled"] = "off"  # idle/disabled plan
    p = e.tick()
    assert p["action"] == "stop" and _switched_off(ha)
    e2, ha2, clock2 = _fault_engine()
    e2.o["stale_minutes"] = 5
    orig = ha2.get_states

    def stale():
        st = orig()
        st["sensor.car_battery_level"]["last_updated"] = "2026-11-03T00:00:00+00:00"
        return st
    ha2.get_states = stale
    clock2["t"] = datetime(2026, 11, 3, 21, 0).astimezone()
    p2 = e2.tick()
    assert p2["action"] == "stop" and p2["reason"] == "wall_connector_fault" and _switched_off(ha2)


def test_032_fault_mid_interval_stops_next_tick():
    e, ha, clock = night({}, **{"switch.car_charge": "on"})
    t0 = datetime(2026, 11, 4, 1, 0)
    clock["t"] = t0
    assert e.tick() is not None  # full plan
    clock["t"] = t0 + timedelta(seconds=30)
    assert e.tick() is None  # nothing new: no plan before the interval
    ha.states["sensor.wc_handle_temp"] = "491"
    n = len(ha.calls)
    clock["t"] = t0 + timedelta(seconds=60)
    p = e.tick()
    assert p is not None and p["action"] == "stop" and p["reason"] == "handle_too_hot"
    assert ("switch", "turn_off", "switch.car_charge", None) in ha.calls[n:]
    assert "fault" in e.status["trigger"]


def _hot_engine():
    e, ha, clock = night({"wall_connector_status": ""}, **{"switch.car_charge": "on"})
    clock["t"] = datetime(2026, 11, 4, 1, 0)
    return e, ha, clock


def _step(e, clock, mins=10):
    clock["t"] += timedelta(minutes=mins)
    return e.tick()


def test_031h_no_cycling_at_limit():
    e, ha, clock = _hot_engine()
    ha.states["sensor.wc_handle_temp"] = "150"
    assert e.tick()["reason"] == "handle_too_hot"
    for temp in ("149", "145", "140", "131", "150", "135"):  # hovering just under the limit
        ha.states["sensor.wc_handle_temp"] = temp
        p = _step(e, clock)
        assert p["action"] == "stop" and p["reason"] == "handle_too_hot", temp
    assert not any(c[1] == "turn_on" and c[0] == "switch" for c in ha.calls)


def test_031h_release_after_cooling():
    e, ha, clock = _hot_engine()
    ha.states["sensor.wc_handle_temp"] = "160"
    e.tick()
    ha.states["sensor.wc_handle_temp"] = "unavailable"  # unknown keeps the latch
    assert _step(e, clock)["reason"] == "handle_too_hot"
    ha.states["sensor.wc_handle_temp"] = "130"  # exactly limit - margin
    p = _step(e, clock)
    assert p["reason"] != "handle_too_hot" and e.hot_latched is False


def test_031h_margin_configurable():
    e, ha, clock = _hot_engine()
    e.o["handle_temp_margin_f"] = 40
    ha.states["sensor.wc_handle_temp"] = "155"
    e.tick()
    ha.states["sensor.wc_handle_temp"] = "120"
    assert _step(e, clock)["reason"] == "handle_too_hot"
    ha.states["sensor.wc_handle_temp"] = "110"
    assert _step(e, clock)["reason"] != "handle_too_hot"


def test_031h_release_on_unplug():
    e, ha, clock = _hot_engine()
    ha.states["sensor.wc_handle_temp"] = "160"
    e.tick()
    ha.states["sensor.wc_handle_temp"] = "145"
    ha.states["binary_sensor.car_charge_cable"] = "off"
    p = _step(e, clock)
    assert e.hot_latched is False and p["reason"] == "unplugged"
    ha.states["binary_sensor.car_charge_cable"] = "on"
    ha.states["switch.car_charge"] = "off"
    assert _step(e, clock)["reason"] != "handle_too_hot"
