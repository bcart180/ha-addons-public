"""0.3.2: card writes reflected immediately, Resume auto restores schedule, vacation mode."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

from engine import VACATION_ENTITY, Engine  # noqa: E402
from server import make_app  # noqa: E402
from test_engine import NOW, FakeHA, lt14, opts, states  # noqa: E402
from test_server import HistHA, _cards_states  # noqa: E402


def lr(j):
    return j["last"]["zones"]["lr"]


@pytest.fixture
def mk(aiohttp_client, monkeypatch):
    async def _mk(o=None, s=None):
        monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
        ha = HistHA(s or _cards_states())
        e = Engine(o or opts(), ha, None, clock=lambda: NOW, localtime=lt14)
        e.evaluate()  # first cycle: app sets lr to 71
        return await aiohttp_client(make_app(e, interval=3600)), ha, e
    return _mk


# ---------- 1) card write shows the new setpoint right away ----------
async def test_setpoint_reply_reflects_write(mk):
    c, ha, _ = await mk()
    j = await (await c.post("/api/zone/lr/setpoint", json={"temperature": 72})).json()
    assert lr(j)["live"]["setpoint"] == 72.0 and lr(j)["reason"] == "manual_override"
    j2 = await (await c.get("/api/state")).json()
    assert lr(j2)["live"]["setpoint"] == 72.0


async def test_mode_and_fan_reply_reflect_write(mk):
    c, _, _ = await mk()
    j = await (await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})).json()
    assert lr(j)["live"]["mode"] == "cool"
    j = await (await c.post("/api/zone/lr/fan", json={"fan_mode": "low"})).json()
    assert lr(j)["live"]["fan_mode"] == "low"
    j = await (await c.post("/api/zone/lr/power", json={"on": False})).json()
    assert lr(j)["live"]["mode"] == "off"


async def test_rejected_write_does_not_touch_snapshot(mk):
    c, _, _ = await mk()
    before = lr(await (await c.get("/api/state")).json())["live"]["setpoint"]
    assert (await c.post("/api/zone/lr/setpoint", json={"temperature": 99})).status == 400
    assert lr(await (await c.get("/api/state")).json())["live"]["setpoint"] == before


# ---------- 2) Resume auto restores the scheduled setpoint, no self re-hold ----------
async def test_resume_restores_schedule_without_rehold(mk):
    c, ha, _ = await mk()
    await c.post("/api/zone/lr/setpoint", json={"temperature": 72})
    ha.calls.clear()
    j = await (await c.post("/api/zone/lr/resume", json={})).json()
    z = next(z for z in j["zones"] if z["name"] == "lr")
    assert z["override_until"] is None
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 71.0}) in ha.calls
    assert lr(j)["live"]["setpoint"] == 71.0 and lr(j)["reason"] == "ok"


async def test_resume_then_next_cycle_stays_auto(mk):
    c, ha, e = await mk()
    await c.post("/api/zone/lr/setpoint", json={"temperature": 72})
    await c.post("/api/zone/lr/resume", json={})
    e.evaluate()
    assert not e.st["override_until"].get("lr")


def test_person_change_in_ha_still_starts_hold():
    ha = FakeHA(states())
    e = Engine(opts(), ha, None, clock=lambda: NOW, localtime=lt14)
    e.evaluate()
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, temperature=74))
    r = e.evaluate()
    assert r["lr"]["reason"] == "manual_override"


# ---------- 4) vacation mode ----------
def vstates(on=True, bath="heat", **kw):
    s = states(**kw)
    s[VACATION_ENTITY] = ("on" if on else "off", {})
    s["climate.bath"] = (bath, {"temperature": 72, "current_temperature": 68})
    return s


def veng(o=None, s=None):
    ha = HistHA(s or vstates())
    return Engine(o or opts(), ha, None, clock=lambda: NOW, localtime=lt14), ha


def test_vacation_sets_back_splits_to_default_60():
    e, ha = veng()
    r = e.evaluate()
    assert r["lr"]["setpoint"] == 60.0 and r["lr"]["profile"] == "vacation"
    assert r["lr"]["reason"] == "vacation_setback"
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 60.0}) in ha.calls


def test_vacation_setback_is_configurable_and_holds():
    e, ha = veng(opts(vacation_heat=58))
    assert e.evaluate()["lr"]["setpoint"] == 58.0
    r = e.evaluate()
    assert r["lr"]["reason"] == "vacation_at_setback" and r["lr"]["action"] == "hold"


def test_vacation_cools_to_cool_setback_when_hot():
    s = vstates(**{"weather.w": ("sunny", {"temperature": 95, "cloud_coverage": 0}),
                   "climate.lr": ("cool", {"temperature": 74, "current_temperature": 80}),
                   "sensor.lr_t": ("80", {})})
    e, _ = veng(opts(vacation_cool=84), s)
    r = e.evaluate()
    assert r["lr"]["mode"] == "cool" and r["lr"]["setpoint"] == 84.0


def test_vacation_turns_radiant_off_and_never_on():
    e, ha = veng()
    r = e.evaluate()
    assert r["bath"]["mode"] == "off" and r["bath"]["reason"] == "vacation_radiant_off"
    assert ("set_hvac_mode", {"entity_id": "climate.bath", "hvac_mode": "off"}) in ha.calls
    ha.calls.clear()
    e.evaluate()
    assert not [c for c in ha.calls if c[1]["entity_id"] == "climate.bath"]


def test_vacation_leaves_person_off_radiant_alone():
    e, ha = veng(s=vstates(bath="off"))
    r = e.evaluate()
    assert r["bath"]["reason"] == "vacation_radiant_off" and not r["bath"]["applied"]
    assert not [c for c in ha.calls if c[1]["entity_id"] == "climate.bath"]


def test_vacation_respects_person_off_split():
    e, ha = veng(s=vstates(**{"climate.lr": ("off", {"temperature": 70})}))
    r = e.evaluate()
    assert r["lr"]["reason"] == "unit_off"
    assert not [c for c in ha.calls if c[1]["entity_id"] == "climate.lr"]


def test_vacation_dry_run_writes_nothing():
    e, ha = veng(opts(dry_run=True))
    r = e.evaluate()
    assert ha.calls == [] and r["bath"]["reason"] == "vacation_radiant_off"


def test_vacation_off_is_normal_schedule():
    e, _ = veng(s=vstates(on=False))
    r = e.evaluate()
    assert r["lr"]["setpoint"] == 71.0 and r["lr"]["profile"] != "vacation"
    assert e.snapshot()["vacation"] is False


def test_vacation_snapshot_shows_mode_and_targets():
    e, _ = veng()
    e.evaluate()
    s = e.snapshot()
    assert s["vacation"] is True and s["vacation_heat"] == 60.0 and s["vacation_cool"] == 82.0


def test_vacation_radiant_cap_still_85():
    e, _ = veng(opts(radiant_setpoint_max=95, radiant_air_shutoff=95))
    assert e.radiant_setpoint_max == 85.0 and e.radiant_room_cap == 85.0


def test_vacation_never_touches_tesla():
    s = vstates()
    s["climate.p3arl2_climate_2"] = ("heat", {"temperature": 70})
    z = [*opts()["zones"], {"name": "t", "label": "T", "climate": "climate.p3arl2_climate_2",
                            "facing_az": 0, "solar_factor": 0, "heat_only": False, "enabled": True}]
    e, ha = veng(opts(zones=z), s)
    e.evaluate()
    assert not [c for c in ha.calls if "p3arl2" in c[1]["entity_id"]]


async def test_card_cannot_turn_radiant_on_in_vacation(mk):
    c, ha, e = await mk(s=vstates(bath="off"))
    ha.calls.clear()
    assert (await c.post("/api/zone/bath/power", json={"on": True})).status == 409
    assert (await c.post("/api/zone/bath/mode", json={"hvac_mode": "heat"})).status == 409
    assert ha.calls == []
    # splits stay writable during vacation (a person may still hold a room)
    assert (await c.post("/api/zone/lr/setpoint", json={"temperature": 62})).status == 200


def test_vacation_options_in_config():
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    assert "\n  vacation_heat: 60\n" in raw and "\n  vacation_cool: 82\n" in raw
    assert "vacation_heat: float(50,68)?" in raw and 'version: "0.8.0"' in raw


# ---------- 3) native Lovelace card ships with the add-on ----------
def test_lovelace_card_routes_writes_through_addon_only():
    src = open(os.path.join(os.path.dirname(__file__), "..", "lovelace", "smart-hvac-card.js")).read()
    assert "callService" not in src  # MA M1: no direct HA service calls from the browser
    assert "<iframe" not in src and "RADIANT_CAP = 85;" in src
    assert "supervisor/ingress/session" in src and "api/zone/${name}/${what}" in src


def test_index_has_vacation_labels():
    html = open(os.path.join(os.path.dirname(__file__), "..", "rootfs", "app", "static", "index.html")).read()
    for k in ("vacation_setback", "vacation_at_setback", "vacation_radiant_off"):
        assert k + ":" in html


async def test_card_routed_write_hits_addon_gates(mk):
    # The card POSTs these exact endpoints through ingress; the add-on's gates apply to them.
    c, ha, _ = await mk(s=vstates(bath="off"))
    ha.calls.clear()
    assert (await c.post("/api/zone/bath_floor/setpoint", json={"temperature": 72})).status == 404
    assert (await c.post("/api/zone/bath/mode", json={"hvac_mode": "heat"})).status == 409
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 95})).status == 400
    assert ha.calls == []


async def test_card_routed_write_refused_in_dry_run(aiohttp_client, monkeypatch):
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    ha = HistHA(_cards_states())
    e = Engine(opts(dry_run=True), ha, None, clock=lambda: NOW, localtime=lt14)
    c = await aiohttp_client(make_app(e, interval=3600))
    assert (await c.post("/api/zone/lr/setpoint", json={"temperature": 72})).status == 409
    assert ha.calls == []
