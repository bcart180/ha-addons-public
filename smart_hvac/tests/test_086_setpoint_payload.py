"""0.8.6: set_temperature payload from supported_features; clamp, step, skip unchanged; setpoint_climate."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import core  # noqa: E402
import test_engine as TE  # noqa: E402

SINGLE = {"supported_features": 425, "min_temp": 50, "max_temp": 88, "temperature": 67}
RANGE = {"supported_features": 426, "min_temp": 50.0, "max_temp": 88.0, "target_temp_step": 1.0,
         "target_temp_low": 67.0, "target_temp_high": None}


def test_single_setpoint_sends_temperature_only():
    data, t, why = core.setpoint_payload(SINGLE, "heat", 71.0)
    assert data == {"temperature": 71.0} and why == "ok"
    assert "target_temp_high" not in data and "hvac_mode" not in data


def test_clamped_to_min_max():
    assert core.setpoint_payload(SINGLE, "heat", 95.0)[0] == {"temperature": 88.0}
    assert core.setpoint_payload(SINGLE, "heat", 40.0)[0] == {"temperature": 50.0}


def test_rounded_to_step_half_up():
    a = dict(SINGLE, target_temp_step=1.0)
    assert core.setpoint_payload(a, "heat", 70.5)[0] == {"temperature": 71.0}
    a = dict(SINGLE, target_temp_step=0.5)
    assert core.setpoint_payload(a, "heat", 70.3)[0] == {"temperature": 70.5}


def test_unchanged_skips_write():
    data, t, why = core.setpoint_payload(SINGLE, "heat", 67.0)
    assert data is None and why == "unchanged" and t == 67.0
    data, _, why = core.setpoint_payload(dict(RANGE, target_temp_low=71.0), "heat", 70.5)
    assert data is None and why == "unchanged"


def test_range_only_heat_keeps_high_inside_native_range():
    # live bug: high was null -> proxy fell back to 88 F = 31.11 C > device max 31.0 C -> HTTP 400
    data, _, _ = core.setpoint_payload(RANGE, "heat", 71.0)
    assert data == {"target_temp_low": 71.0, "target_temp_high": 87.0}
    assert data["target_temp_high"] < RANGE["max_temp"]


def test_range_only_keeps_existing_other_bound():
    data, _, _ = core.setpoint_payload(dict(RANGE, target_temp_high=80.0), "heat", 71.0)
    assert data == {"target_temp_low": 71.0, "target_temp_high": 80.0}
    data, _, _ = core.setpoint_payload(dict(RANGE, target_temp_low=65.0, target_temp_high=78.0), "cool", 76.0)
    assert data == {"target_temp_low": 65.0, "target_temp_high": 76.0}


def test_range_cool_low_falls_inside_min():
    data, _, _ = core.setpoint_payload(dict(RANGE, target_temp_low=None), "cool", 76.0)
    assert data == {"target_temp_low": 51.0, "target_temp_high": 76.0}


def test_no_setpoint_feature_no_write():
    assert core.setpoint_payload({"supported_features": 384}, "heat", 70.0)[0] is None
    assert core.setpoint_payload(dict(RANGE), "fan_only", 70.0)[2] == "range_mode_unsupported"


def test_legacy_entity_without_attributes_unchanged_behaviour():
    assert core.setpoint_payload({}, "heat", 70.5) == ({"temperature": 70.5}, 70.5, "ok")


class RangeHA(TE.FakeHA):
    def call(self, domain, service, data):
        self.calls.append((service, data))
        st, at = self.s[data["entity_id"]]
        if service == "set_hvac_mode":
            self.s[data["entity_id"]] = (data["hvac_mode"], at)
        else:
            self.s[data["entity_id"]] = (st, dict(at, **{k: v for k, v in data.items() if k != "entity_id"}))


def test_engine_writes_setpoint_climate_with_range_payload():
    from engine import Engine
    s = TE.states()
    s["climate.lr_source_555"] = ("heat", dict(RANGE))
    o = TE.opts(zones=[dict(z) for z in TE.ZONES])
    for z in o["zones"]:
        if z["climate"] == "climate.lr":
            z["setpoint_climate"] = "climate.lr_source_555"
    ha = RangeHA(s)
    e = Engine(o, ha, None, clock=lambda: TE.NOW, localtime=TE.lt14)
    e.evaluate()
    sp = [d for sv, d in ha.calls if sv == "set_temperature" and d["entity_id"] == "climate.lr_source_555"]
    assert sp and set(sp[0]) == {"entity_id", "target_temp_low", "target_temp_high"}
    assert sp[0]["target_temp_high"] <= 87.0
    assert not [d for sv, d in ha.calls if sv == "set_temperature" and d["entity_id"] == "climate.lr"]
    assert not [d for sv, d in ha.calls if sv == "set_temperature" and "hvac_mode" in d]


def test_engine_skips_unchanged_setpoint_write():
    from engine import Engine
    ha = TE.FakeHA(TE.states())
    e = Engine(TE.opts(), ha, None, clock=lambda: TE.NOW, localtime=TE.lt14)
    e.evaluate()
    n = len([c for c in ha.calls if c[0] == "set_temperature"])
    r = e.evaluate()  # live setpoints now equal the targets
    assert len([c for c in ha.calls if c[0] == "set_temperature"]) == n
    assert all(v.get("reason") != "setpoint_write_failed" for v in r.values())


def test_setpoint_climate_excluded_entity_falls_back():
    from engine import Engine
    e = Engine(TE.opts(), TE.FakeHA(TE.states()), None, clock=lambda: TE.NOW, localtime=TE.lt14)
    z = {"climate": "climate.lr", "setpoint_climate": "climate.p3arl2_climate_555"}
    assert e.setpoint_target(z) == "climate.lr"
