"""0.8.8: evaluate-level tests for indoor_stale_min and the unit-temperature plausibility range (SA L1/L2)."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
import pytest  # noqa: E402
import test_engine as T  # noqa: E402

import core  # noqa: E402
from engine import Engine  # noqa: E402

OLD = datetime.fromtimestamp(T.NOW - 7200, timezone.utc).isoformat()


class StampHA(T.FakeHA):
    def get_states(self):
        out = super().get_states()
        for r in out:
            if r["entity_id"] == "sensor.lr_t":
                r["last_reported"] = OLD
        return out


@pytest.fixture(autouse=True)
def seen(monkeypatch):
    got = {}  # first call per cycle is the control decision; later ones are the forecast
    real = core.decide

    def spy(inp, z, cfg):
        if z.get("name") == "lr":
            got.setdefault("indoor", inp.get("indoor_temp"))
        return real(inp, z, cfg)
    monkeypatch.setattr(core, "decide", spy)
    return got


def run(stale_min=None, unit_t=60, unit_extra=None, room="66"):
    zones = [dict(z) for z in T.ZONES]
    if stale_min is not None:
        zones[0]["indoor_stale_min"] = stale_min
    at = {"temperature": 65, "current_temperature": unit_t}
    at.update(unit_extra or {})
    ha = StampHA(T.states(**{"climate.lr": ("heat", at), "sensor.lr_t": (room, {})}))
    e = Engine(T.opts(zones=zones), ha, None, clock=lambda: T.NOW, localtime=T.lt14)
    r = e.evaluate()["lr"]
    lr_calls = [c for c in ha.calls if c[1].get("entity_id") == "climate.lr"]
    return r, lr_calls, e


def test_option_off_uses_frozen_room_sensor_as_before(seen):
    off, _, _ = run(None, unit_t=60, room="80")
    assert seen.pop("indoor") == 80.0
    zero, _, _ = run(0, unit_t=60, room="80")
    assert seen["indoor"] == 80.0
    assert off["action"] == zero["action"] == "set" and off["setpoint"] == zero["setpoint"]


def test_stale_sensor_uses_unit_temp(seen):
    r, _, _ = run(60, unit_t=60, room="80")
    assert seen["indoor"] == 60.0 and r["action"] == "set"


@pytest.mark.parametrize("unit_t,extra", [(None, None), (5, None), (150, None), (-40, None),
                                          (45, {"temperature_unit": "C"}), (-5, {"temperature_unit": "°C"})])
def test_stale_plus_missing_or_implausible_unit_temp_holds(unit_t, extra):
    r, calls, _ = run(60, unit_t=unit_t, unit_extra=extra)
    assert r["action"] == "hold" and r["reason"] == "missing:indoor_temp" and calls == []


@pytest.mark.parametrize("unit_t,extra", [(60, None), (20, {"temperature_unit": "C"})])
def test_stale_plausible_unit_temp_used(unit_t, extra, seen):
    r, _, _ = run(60, unit_t=unit_t, unit_extra=extra)
    assert r.get("reason") != "missing:indoor_temp" and seen["indoor"] == unit_t


@pytest.mark.parametrize("t,unit,want", [(29.9, None, None), (30, None, 30), (110, None, 110), (110.1, None, None),
                                         (-1.2, "C", None), (0, "C", 0), (43.3, "C", 43.3), (44, "C", None),
                                         ("bad", None, None)])
def test_unit_temp_range(t, unit, want):
    a = {"current_temperature": t}
    if unit:
        a["temperature_unit"] = unit
    assert Engine._unit_temp(a) == want
