"""Engine scenarios with a fake HA: presence, temperature, sun, safety gates."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
import pytest  # noqa: E402

from engine import Engine  # noqa: E402

NOW = 1_800_000_000
ZONES = [
    {"name": "lr", "label": "Room A", "climate": "climate.lr", "indoor": "sensor.lr_t", "humidity": "sensor.lr_h",
     "facing_az": 225, "solar_factor": 0.8, "heat_only": False, "enabled": True},
    {"name": "bath", "label": "Bath", "climate": "climate.bath", "indoor": "sensor.bath_t",
     "facing_az": 90, "solar_factor": 0, "heat_only": True, "enabled": True, "comfort": 72, "sleep": 66, "away": 60},
]


class FakeHA:
    def __init__(self, states):
        self.s = states
        self.calls = []

    def get_states(self):
        return [{"entity_id": k, "state": v[0], "attributes": v[1]} for k, v in self.s.items()]

    def call(self, domain, service, data):
        self.calls.append((service, data))
        st, at = self.s[data["entity_id"]]
        if service == "set_hvac_mode":
            self.s[data["entity_id"]] = (data["hvac_mode"], at)
        else:
            self.s[data["entity_id"]] = (st, dict(at, temperature=data["temperature"]))


def states(**kw):
    s = {
        "device_tracker.phone": ("home", {}),
        "weather.w": ("cloudy", {"temperature": 45, "cloud_coverage": 100}),
        "sun.sun": ("below_horizon", {"elevation": -10, "azimuth": 0}),
        "climate.lr": ("heat", {"temperature": 65, "current_temperature": 66}),
        "sensor.lr_t": ("66", {}), "sensor.lr_h": ("40", {}),
        "climate.bath": ("heat", {"temperature": 60, "current_temperature": 64}),
        "sensor.bath_t": ("64", {}),
    }
    s.update(kw)
    return s


def opts(**kw):
    o = {"enabled": True, "dry_run": False, "presence_entity": "device_tracker.phone", "weather_entity": "weather.w",
         "min_cycle_min": 15, "override_hours": 2, "profiles": {"home": 71, "guest": 71}, "zones": ZONES}
    o.update(kw)
    return o


def lt14(_t=None):
    return time.struct_time((2026, 10, 30, 14, 0, 0, 4, 303, 0))


def eng(o=None, s=None, tmp=None, clock=lambda: NOW):
    ha = FakeHA(s or states())
    return Engine(o or opts(), ha, tmp, clock=clock, localtime=lt14), ha


def test_home_heats_room_a_to_71_and_bath_to_comfort():
    e, ha = eng()
    r = e.evaluate()
    assert r["lr"]["setpoint"] == 71.0 and r["lr"]["applied"]
    assert r["bath"]["setpoint"] == 72.0
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 71.0}) in ha.calls


def test_master_off_never_writes():
    e, ha = eng(opts(enabled=False, dry_run=False))
    e.evaluate()
    assert ha.calls == []


def test_dry_run_default_never_writes_but_reports():
    o = opts()
    del o["dry_run"]
    e, ha = eng(o)
    r = e.evaluate()
    assert ha.calls == [] and r["lr"]["action"] == "set" and not r["lr"]["applied"]


def test_defaults_are_safe():
    e, _ = eng({"zones": []})
    assert e.enabled is False and e.dry_run is True


def test_away_setback_uses_only_presence_entity():
    s = states(**{"device_tracker.phone": ("not_home", {}), "device_tracker.person_b": ("home", {})})
    e, _ = eng(s=s)
    r = e.evaluate()
    assert r["lr"]["profile"] == "away" and r["lr"]["setpoint"] == 65.0
    assert r["bath"]["setpoint"] == 60.0


def test_guest_profile_keeps_71_when_owner_away():
    s = states(**{"device_tracker.phone": ("not_home", {})})
    e, _ = eng(s=s)
    e.set_profile("guest")
    assert e.evaluate()["lr"]["setpoint"] == 71.0


def test_missing_presence_holds():
    s = states(**{"device_tracker.phone": ("unavailable", {})})
    e, ha = eng(s=s)
    r = e.evaluate()
    assert r["lr"]["reason"] == "missing:presence" and ha.calls == []


def test_missing_weather_holds():
    s = states(**{"weather.w": ("unavailable", {})})
    e, ha = eng(s=s)
    assert e.evaluate()["lr"]["reason"] == "missing:outdoor_temp" and ha.calls == []


def test_sun_on_west_room_lowers_heat_target():
    s = states(**{"sun.sun": ("above_horizon", {"elevation": 40, "azimuth": 225}),
                  "weather.w": ("sunny", {"temperature": 45, "cloud_coverage": 0})})
    e, _ = eng(s=s)
    sp = e.evaluate()["lr"]["setpoint"]
    assert 69.5 <= sp < 71.0


def test_m1_person_off_is_respected():
    s = states(**{"climate.lr": ("off", {"temperature": 65, "current_temperature": 60})})
    e, ha = eng(s=s)
    r = e.evaluate()
    assert r["lr"]["reason"] == "unit_off"
    assert not any(c[1]["entity_id"] == "climate.lr" for c in ha.calls)


def test_radiant_overtemp_shutoff():
    s = states(**{"sensor.bath_t": ("87", {})})
    e, ha = eng(s=s)
    r = e.evaluate()
    assert r["bath"]["reason"] == "overtemp_shutoff"
    assert ("set_hvac_mode", {"entity_id": "climate.bath", "hvac_mode": "off"}) in ha.calls
    ha.calls.clear()
    r = e.evaluate()  # now off by the app's safety cut -> stays off (not app_turned_off)
    assert r["bath"]["reason"] == "unit_off" and ha.calls == []


def test_radiant_overtemp_no_write_in_dry_run():
    s = states(**{"sensor.bath_t": ("87", {})})
    e, ha = eng(opts(dry_run=True), s=s)
    assert e.evaluate()["bath"]["reason"] == "overtemp_shutoff" and ha.calls == []


def test_manual_change_starts_override_then_auto_resumes():
    t = [NOW]
    e, ha = eng(clock=lambda: t[0])
    e.evaluate()
    ha.s["climate.lr"] = ("heat", dict(ha.s["climate.lr"][1], temperature=75))  # person sets 75 in HA
    t[0] += 300
    ha.calls.clear()
    assert e.evaluate()["lr"]["reason"] == "manual_override" and ha.calls == []
    t[0] += 2 * 3600 + 1
    r = e.evaluate()
    assert r["lr"]["reason"] == "ok" and r["lr"]["setpoint"] == 71.0


def test_short_cycle_guard():
    s = states(**{"sensor.lr_t": ("76", {}), "weather.w": ("sunny", {"temperature": 85, "cloud_coverage": 100})})
    e, ha = eng(s=s)
    e.st["last_change"]["lr"] = NOW - 60
    assert e.evaluate()["lr"]["reason"] == "short_cycle_guard"
    assert not any(c[1]["entity_id"] == "climate.lr" for c in ha.calls)


def test_zone_disabled_is_untouched():
    z = [dict(ZONES[0], enabled=False)]
    e, ha = eng(opts(zones=z))
    assert e.evaluate()["lr"]["reason"] == "zone_disabled" and ha.calls == []


def test_state_persists_but_app_off_resets(tmp_path):
    p = str(tmp_path / "s.json")
    e, _ = eng(tmp=p)
    e.set_profile("sleep")
    e.st["app_off"] = ["lr"]
    e.save()
    e2, _ = eng(tmp=p)
    assert e2.st["profile"] == "sleep" and e2.st["app_off"] == []


def test_bad_profile_rejected():
    e, _ = eng()
    with pytest.raises(ValueError):
        e.set_profile("party")


def test_ha_client_allows_only_climate_services():
    from ha_client import HAClient
    with pytest.raises(PermissionError):
        HAClient(token="x").call("lock", "unlock", {})


def test_tesla_climate_zone_is_never_touched():
    z = [dict(ZONES[0], name="car", climate="climate.p3arl2_climate")]
    s = states(**{"climate.p3arl2_climate": ("off", {"temperature": 70})})
    e, ha = eng(opts(zones=z), s=s)
    assert e.evaluate()["car"]["reason"] == "excluded_entity" and ha.calls == []


def test_ha_client_refuses_tesla_and_non_climate_entities():
    from ha_client import HAClient
    for eid in ("climate.p3arl2_climate", "switch.heater", ""):
        with pytest.raises(PermissionError):
            HAClient(token="x").call("climate", "set_temperature", {"entity_id": eid, "temperature": 70})


def _multi(**st):
    o = opts(presence_entities=["device_tracker.phone", "device_tracker.person_b", ""])
    return eng(o, s=states(**st))


def test_person_b_home_counts_as_occupied():
    e, _ = _multi(**{"device_tracker.phone": ("not_home", {}), "device_tracker.person_b": ("home", {})})
    r = e.evaluate()["lr"]
    assert r["profile"] == "home" and r["setpoint"] == 71.0


def test_everyone_away_sets_back():
    e, _ = _multi(**{"device_tracker.phone": ("not_home", {}), "device_tracker.person_b": ("not_home", {})})
    assert e.evaluate()["lr"]["profile"] == "away"


def test_one_unavailable_uses_the_other():
    e, _ = _multi(**{"device_tracker.phone": ("unavailable", {}), "device_tracker.person_b": ("not_home", {})})
    assert e.evaluate()["lr"]["profile"] == "away"


def test_all_unavailable_holds():
    e, ha = _multi(**{"device_tracker.phone": ("unavailable", {})})  # person_b entity missing entirely
    assert e.evaluate()["lr"]["reason"] == "missing:presence" and ha.calls == []


def test_blank_person_c_slot_ignored_and_legacy_merged():
    e, _ = eng(opts(presence_entities=["device_tracker.person_b", ""]))
    assert e.presence_entities() == ["device_tracker.phone", "device_tracker.person_b"]


def test_unlisted_phone_never_counts():
    e, _ = _multi(**{"device_tracker.phone": ("not_home", {}), "device_tracker.person_b": ("not_home", {}),
                     "device_tracker.person_c": ("home", {})})
    assert e.evaluate()["lr"]["profile"] == "away"
