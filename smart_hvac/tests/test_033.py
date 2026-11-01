"""0.3.3: a unit that rounds the app's write (60 -> 61) is not a manual override; vacation off restores 71."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

from engine import OWN_WRITE_TOLERANCE_F, VACATION_ENTITY, Engine  # noqa: E402
from test_engine import NOW, FakeHA, lt14, opts, states  # noqa: E402


class RoundingHA(FakeHA):
    """Celsius-native split: a 60°F write reads back as 61°F (15.6°C -> 16°C)."""

    def call(self, domain, service, data):
        super().call(domain, service, data)
        if service == "set_temperature" and data["temperature"] == 60:
            st, at = self.s[data["entity_id"]]
            self.s[data["entity_id"]] = (st, dict(at, temperature=61))


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


def setup(vac=True):
    s = states()  # lr starts at 65 so the app's first cycle writes (71 at home, 60 on vacation)
    s[VACATION_ENTITY] = ("on" if vac else "off", {})
    ha, clk = RoundingHA(s), Clock()
    return Engine(opts(), ha, None, clock=clk, localtime=lt14), ha, clk


def set_vac(ha, on):
    ha.s[VACATION_ENTITY] = ("on" if on else "off", {})


def test_tolerance_is_one_degree():
    assert OWN_WRITE_TOLERANCE_F == 1.0


def test_rounded_setback_60_to_61_is_own_write_and_vacation_off_restores_71():
    e, ha, clk = setup(vac=True)
    r = e.evaluate()
    assert r["lr"]["setpoint"] == 60.0 and r["lr"]["reason"] == "vacation_setback" and r["lr"]["applied"]
    assert ha.s["climate.lr"][1]["temperature"] == 61  # the unit rounded it
    clk.t += 300
    ha.calls.clear()
    r = e.evaluate()  # still on vacation: no hold, no re-send of the setback every cycle
    assert r["lr"]["reason"] == "vacation_at_setback" and r["lr"]["action"] == "hold"
    assert ha.calls == [] and e.snapshot()["zones"][0]["override_until"] is None
    set_vac(ha, False)
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "ok" and r["lr"]["setpoint"] == 71.0 and r["lr"]["applied"]
    assert ha.s["climate.lr"][1]["temperature"] == 71
    assert e.snapshot()["zones"][0]["override_until"] is None
    clk.t += 300
    r = e.evaluate()  # and it stays in auto
    assert r["lr"]["reason"] == "no_change" and e.snapshot()["zones"][0]["override_until"] is None


def test_vacation_off_on_the_very_next_cycle_after_rounding_restores_71():
    e, ha, clk = setup(vac=True)
    e.evaluate()
    set_vac(ha, False)
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["setpoint"] == 71.0 and r["lr"]["reason"] == "ok"
    assert e.snapshot()["zones"][0]["override_until"] is None


def test_real_manual_change_of_2f_during_vacation_still_holds():
    e, ha, clk = setup(vac=True)
    e.evaluate()  # app writes 60, unit reports 61
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, temperature=62))  # a person sets 62 (2°F from the app's 60)
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "manual_override" and r["lr"]["action"] == "hold"
    assert e.snapshot()["zones"][0]["override_until"] is not None
    set_vac(ha, False)
    clk.t += 300
    ha.calls.clear()
    r = e.evaluate()  # the person's hold is respected after vacation ends too
    assert r["lr"]["reason"] == "manual_override" and [c for c in ha.calls if c[1]["entity_id"] == "climate.lr"] == []


def test_real_manual_change_of_2f_outside_vacation_still_holds():
    e, ha, clk = setup(vac=False)
    e.evaluate()
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, temperature=73))
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "manual_override" and e.snapshot()["zones"][0]["override_until"] is not None


def test_1f_drift_from_app_write_is_not_a_hold():
    e, ha, clk = setup(vac=False)
    e.evaluate()  # app at 71
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, temperature=72))
    clk.t += 300
    r = e.evaluate()
    assert r["lr"]["reason"] == "no_change" and e.snapshot()["zones"][0]["override_until"] is None


def test_radiant_cap_defaults_to_85_and_is_an_option():
    import core
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    assert "\n  radiant_setpoint_max: 85\n" in raw and "\n  radiant_air_shutoff: 80\n" in raw
    assert core.DEFAULTS["radiant_room_cap"] == 80.0
    e, _, _ = setup(vac=False)
    assert e.radiant_setpoint_max == 85.0 and e.radiant_room_cap == 80.0
    e.opts["radiant_air_shutoff"] = 78
    assert e.radiant_room_cap == 78.0  # lower is allowed
    e.opts["radiant_air_shutoff"] = 95
    assert e.radiant_room_cap == 85.0  # never above 85


def test_tolerance_boundary_inclusive_at_exactly_1f_and_manual_above():
    for reported, held in ((72.0, False), (72.5, True)):  # app wrote 71
        e, ha, clk = setup(vac=False)
        e.evaluate()
        st, at = ha.s["climate.lr"]
        ha.s["climate.lr"] = (st, dict(at, temperature=reported))
        clk.t += 300
        r = e.evaluate()
        assert (r["lr"]["reason"] == "manual_override") is held
