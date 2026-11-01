"""Ingress GUI/API: ingress-only access, dry-run refuses writes, profile one-tap."""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

from engine import Engine  # noqa: E402
from server import _NoHealthAccess, make_app  # noqa: E402
from test_engine import FakeHA, lt14, opts, states  # noqa: E402


@pytest.fixture
def mk(aiohttp_client, monkeypatch):
    async def _mk(o, allow="127.0.0.1"):
        monkeypatch.setenv("SH_ALLOW", allow)
        ha = FakeHA(states())
        e = Engine(o, ha, None, clock=lambda: 1_800_000_000, localtime=lt14)
        return await aiohttp_client(make_app(e, interval=3600)), ha
    return _mk


async def test_non_ingress_rejected(mk):
    c, _ = await mk(opts(), allow="172.30.32.2")
    assert (await c.get("/api/state")).status == 403


async def test_state_and_index(mk):
    c, _ = await mk(opts())
    assert (await c.get("/")).status == 200
    j = await (await c.get("/api/state")).json()
    assert {z["name"] for z in j["zones"]} == {"lr", "bath"}


async def test_profile_one_tap(mk):
    c, _ = await mk(opts())
    j = await (await c.post("/api/profile", json={"profile": "guest"})).json()
    assert j["profile"] == "guest"
    assert (await c.post("/api/profile", json={"profile": "x"})).status == 400


async def test_setpoint_refused_in_dry_run(mk):
    c, ha = await mk(opts(dry_run=True))
    r = await c.post("/api/zone/lr/setpoint", json={"temperature": 72})
    assert r.status == 409 and ha.calls == []


async def test_setpoint_live_starts_override_and_bounds(mk):
    c, ha = await mk(opts())
    assert (await c.post("/api/zone/lr/setpoint", json={"temperature": 99})).status == 400
    j = await (await c.post("/api/zone/lr/setpoint", json={"temperature": 73})).json()
    assert next(z for z in j["zones"] if z["name"] == "lr")["override_until"]
    assert ("set_temperature", {"entity_id": "climate.lr", "temperature": 73.0}) in ha.calls


async def test_empty_allow_env_still_ingress_only(mk):
    c, _ = await mk(opts(), allow="")
    assert (await c.get("/api/state")).status == 403


async def test_health_open_to_localhost_only(mk):
    c, _ = await mk(opts(), allow="172.30.32.2")  # test client connects from 127.0.0.1
    assert (await c.get("/api/health")).status == 200
    assert (await c.get("/api/state")).status == 403
    assert (await c.post("/api/health")).status == 403


async def test_post_requires_json_content_type(mk):
    c, ha = await mk(opts())
    ha.calls.clear()
    r = await c.post("/api/profile", data="profile=guest", headers={"Content-Type": "text/plain"})
    assert r.status == 400
    r = await c.post("/api/zone/lr/setpoint", data='{"temperature": 72}', headers={"Content-Type": "text/plain"})
    assert r.status == 400 and ha.calls == []
    assert (await c.post("/api/zone/lr/resume", data="x", headers={"Content-Type": "text/plain"})).status == 400


async def test_malformed_body_is_400_not_500(mk):
    c, ha = await mk(opts())
    ha.calls.clear()
    for body in ("[]", "123", "{not json", '"x"'):
        h = {"Content-Type": "application/json"}
        assert (await c.post("/api/profile", data=body, headers=h)).status == 400
        assert (await c.post("/api/zone/lr/setpoint", data=body, headers=h)).status == 400
    assert ha.calls == []


def test_panel_is_admin_only():
    cfg = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    # Supervisor default panel_admin is true (admin-only); it must never be set false.
    assert "\npanel_admin:" not in cfg and "ingress_port" not in cfg and "\nports:" not in cfg


def test_presence_schema_restricts_to_trackers_and_persons():
    import re
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    pat = r"match(^(device_tracker|person)\.[a-z0-9_]+$)"
    assert "\n  presence_entity: " + pat + "?\n" in raw
    assert "\n  presence_entities:\n    - " + pat + "\n" in raw
    rx = re.compile(pat[len("match("):-1])
    for good in ("device_tracker.person_a_phone", "device_tracker.phone_2", "person.person_b"):
        assert rx.match(good)
    for bad in ("group.family", "lock.front_door", "device_tracker.X", "device_tracker.a b", "binary_sensor.x", ""):
        assert not rx.match(bad)


# ---------- 0.3.0 thermostat cards: mode / power / fan / hold / resume / history ----------
def _cards_states():
    s = states()
    s["climate.lr"] = ("heat", {"temperature": 70, "current_temperature": 69, "hvac_modes": ["off", "heat", "cool"],
                                "fan_modes": ["auto", "low", "high"], "fan_mode": "auto", "hvac_action": "heating"})
    s["climate.bath"] = ("heat", {"temperature": 70, "current_temperature": 68, "hvac_modes": ["off", "heat"]})
    s["automation.radiant_heat_timeout_90_min"] = ("on", {})
    return s


class HistHA(FakeHA):
    def __init__(self, s):
        super().__init__(s)
        self.hist_calls = []

    def call(self, domain, service, data):
        from ha_client import ALLOWED_SERVICES, excluded
        assert domain == "climate" and service in ALLOWED_SERVICES and not excluded(data["entity_id"])
        self.calls.append((service, data))
        st, at = self.s[data["entity_id"]]
        if service == "set_hvac_mode":
            self.s[data["entity_id"]] = (data["hvac_mode"], at)
        elif service == "set_fan_mode":
            self.s[data["entity_id"]] = (st, dict(at, fan_mode=data["fan_mode"]))
        else:
            self.s[data["entity_id"]] = (st, dict(at, temperature=data["temperature"]))

    def get_history(self, ents, start):
        self.hist_calls.append((tuple(ents), start))
        return [
            [{"entity_id": "climate.lr", "state": "heat", "last_changed": "2027-01-15T07:00:00+00:00",
              "attributes": {"temperature": 71, "hvac_action": "heating"}},
             {"entity_id": "climate.lr", "state": "heat", "last_changed": "2027-01-15T07:30:00.5+00:00",
              "attributes": {"temperature": 71, "hvac_action": "idle"}},
             {"entity_id": "climate.lr", "state": "off", "last_changed": "2027-01-15T07:45:00+00:00",
              "attributes": {}}],
            [{"entity_id": "sensor.lr_t", "state": "69.5", "last_changed": "2027-01-15T07:00:00+00:00"},
             {"entity_id": "sensor.lr_t", "state": "unavailable", "last_changed": "2027-01-15T07:10:00+00:00"}],
            [{"entity_id": "weather.w", "state": "cloudy", "last_changed": "2027-01-15T07:00:00+00:00",
              "attributes": {"temperature": 41}}],
        ]


@pytest.fixture
def mkc(aiohttp_client, monkeypatch):
    async def _mk(o):
        monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
        ha = HistHA(_cards_states())
        e = Engine(o, ha, None, clock=lambda: 1_800_000_000, localtime=lt14)
        return await aiohttp_client(make_app(e, interval=3600)), ha, e
    return _mk


def _z(j, n):
    return next(z for z in j["zones"] if z["name"] == n)


async def test_mode_change_is_manual_hold_and_validated(mkc):
    c, ha, _ = await mkc(opts())
    ha.calls.clear()
    j = await (await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})).json()
    assert ("set_hvac_mode", {"entity_id": "climate.lr", "hvac_mode": "cool"}) in ha.calls
    assert _z(j, "lr")["override_until"] == 1_800_000_000 + 2 * 3600
    for bad in ({"hvac_mode": "auto"}, {"hvac_mode": "fan_only"}, {"hvac_mode": 1}, {}):
        assert (await c.post("/api/zone/lr/mode", json=bad)).status == 400
    assert (await c.post("/api/zone/bath/mode", json={"hvac_mode": "cool"})).status == 400  # heat-only


async def test_power_off_on_restores_last_mode(mkc):
    c, ha, _ = await mkc(opts())
    await c.post("/api/zone/lr/mode", json={"hvac_mode": "cool"})
    ha.calls.clear()
    await c.post("/api/zone/lr/power", json={"on": False})
    await c.post("/api/zone/lr/power", json={"on": True})
    assert [x[1]["hvac_mode"] for x in ha.calls] == ["off", "cool"]
    assert (await c.post("/api/zone/lr/power", json={"on": "yes"})).status == 400


async def test_fan_allowlisted_and_validated(mkc):
    c, ha, _ = await mkc(opts())
    ha.calls.clear()
    assert (await c.post("/api/zone/lr/fan", json={"fan_mode": "high"})).status == 200
    assert ("set_fan_mode", {"entity_id": "climate.lr", "fan_mode": "high"}) in ha.calls
    assert (await c.post("/api/zone/lr/fan", json={"fan_mode": "turbo"})).status == 400
    assert (await c.post("/api/zone/bath/fan", json={"fan_mode": "auto"})).status == 400


async def test_card_writes_refused_in_dry_run(mkc):
    c, ha, _ = await mkc(opts(dry_run=True))
    ha.calls.clear()
    for path, body in (("mode", {"hvac_mode": "heat"}), ("power", {"on": False}), ("fan", {"fan_mode": "low"}),
                       ("setpoint", {"temperature": 70})):
        assert (await c.post("/api/zone/lr/" + path, json=body)).status == 409
    assert ha.calls == []


async def test_radiant_setpoint_capped(mkc):
    c, _, _ = await mkc(opts(radiant_setpoint_max=80))
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 81})).status == 400
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 74})).status == 200


async def test_hold_and_resume(mkc):
    c, ha, e = await mkc(opts())
    j = await (await c.post("/api/zone/lr/hold", json={"hours": 4})).json()
    assert _z(j, "lr")["override_until"] == 1_800_000_000 + 4 * 3600
    ha.calls.clear()
    e.evaluate()
    assert ha.calls == []  # app respects the hold
    for bad in ({"hours": 0.1}, {"hours": 13}, {"hours": "2"}, {"hours": True}):
        assert (await c.post("/api/zone/lr/hold", json=bad)).status == 400
    assert (await c.post("/api/zone/nope/hold", json={})).status == 404
    j = await (await c.post("/api/zone/lr/resume", json={})).json()
    assert _z(j, "lr")["override_until"] is None
    assert (await c.post("/api/zone/nope/resume", json={})).status == 404


async def test_card_writes_need_json(mkc):
    c, ha, _ = await mkc(opts())
    ha.calls.clear()
    for p in ("mode", "power", "fan", "hold"):
        assert (await c.post("/api/zone/lr/" + p, data="x", headers={"Content-Type": "text/plain"})).status == 400
        h = {"Content-Type": "application/json"}
        assert (await c.post("/api/zone/lr/" + p, data="[]", headers=h)).status == 400
    assert ha.calls == []


async def test_state_exposes_card_fields(mkc):
    c, _, e = await mkc(opts())
    e.evaluate()
    j = await (await c.get("/api/state")).json()
    lv = j["last"]["zones"]["lr"]["live"]
    assert lv["fan_modes"] == ["auto", "low", "high"] and lv["hvac_action"] == "heating"
    assert j["last"]["radiant_timeout"] == "on" and j["radiant_room_cap"] and j["override_hours"] == 2


async def test_history_endpoint(mkc):
    c, ha, _ = await mkc(opts())
    r = await c.get("/api/zone/lr/history?range=24h")
    assert r.status == 200
    j = await r.json()
    assert ha.hist_calls[0][0] == ("climate.lr", "sensor.lr_t", "weather.w")
    assert ha.hist_calls[0][1].endswith("+00:00")
    assert len(j["indoor"]) == 1 and j["indoor"][0][1] == 69.5  # unavailable dropped
    assert [h[1] for h in j["heating"]] == [1, 0, 0]
    assert [s[1] for s in j["setpoint"]] == [71, 71, None]
    assert j["outdoor"][0][1] == 41 and j["end"] - j["start"] == 86400
    j7 = await (await c.get("/api/zone/lr/history?range=7d")).json()
    assert j7["end"] - j7["start"] == 7 * 86400
    assert (await c.get("/api/zone/lr/history?range=1y")).status == 400
    assert (await c.get("/api/zone/nope/history")).status == 404
    assert not any(x for x in ha.calls if x[0] not in ("set_hvac_mode", "set_temperature"))  # read-only


async def test_history_failure_is_502_not_500(mkc):
    c, ha, _ = await mkc(opts())

    def boom(*_a):
        raise OSError("down")
    ha.get_history = boom
    assert (await c.get("/api/zone/lr/history")).status == 502


async def test_history_downsampled(mkc):
    c, ha, _ = await mkc(opts())
    stamp = "2027-01-15T%02d:%02d:00+00:00"
    rows = [{"entity_id": "sensor.lr_t", "state": "70", "last_changed": stamp % (i // 60, i % 60)}
            for i in range(1000)]
    ha.get_history = lambda *_a: [rows]
    j = await (await c.get("/api/zone/lr/history")).json()
    assert len(j["indoor"]) <= 301


async def test_thermostat_page_served(mkc):
    c, _, _ = await mkc(opts())
    r = await c.get("/thermostat")
    t = await r.text()
    assert r.status == 200 and "Resume auto" in t and "ecobee" not in t.lower()
    assert "schedule" not in t.replace("Schedules live in the app", "")  # cards never write schedules


def test_ha_client_allowlist():
    from ha_client import ALLOWED_SERVICES, HAClient
    assert set(ALLOWED_SERVICES) == {"set_hvac_mode", "set_temperature", "set_fan_mode"}
    cl = HAClient(token="t", base="http://127.0.0.1:9")
    for svc in ("turn_on", "turn_off", "set_preset_mode", "set_temperature_schedule"):
        with pytest.raises(PermissionError):
            cl.call("climate", svc, {"entity_id": "climate.lr"})
    with pytest.raises(PermissionError):
        cl.call("climate", "set_fan_mode", {"entity_id": "climate.p3arl2_hvac", "fan_mode": "low"})
    with pytest.raises(PermissionError):
        cl.call("automation", "turn_off", {"entity_id": "automation.radiant_heat_timeout_90_min"})


# ---------- MA L1 / L2 ----------
def test_radiant_cap_schema_max_85():
    raw = open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")).read()
    assert "\n  radiant_setpoint_max: float(60,85)\n" in raw and "\n  radiant_air_shutoff: float(70,85)\n" in raw
    assert "\n  radiant_room_cap: float(70,85)?\n" in raw  # legacy, accepted so saved options validate


async def test_radiant_cap_clamped_server_side(mkc):
    c, _, e = await mkc(opts(radiant_setpoint_max=90, radiant_air_shutoff=90))
    assert e.radiant_room_cap == 85 and e.radiant_setpoint_max == 85
    assert e._cfg(e.zone("bath"))["radiant_room_cap"] == 85
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 86})).status == 400
    assert (await c.post("/api/zone/bath/setpoint", json={"temperature": 85})).status == 200
    j = await (await c.get("/api/state")).json()
    assert j["radiant_room_cap"] == 85


def test_card_buttons_use_data_attributes_not_js_strings():
    t = open(os.path.join(os.path.dirname(__file__), "..", "rootfs", "app", "static", "thermostat.html")).read()
    assert "this.dataset.fan" in t and 'data-fan="${esc(f)}"' in t
    assert "fan_mode:'${" not in t and "hvac_mode:'${" not in t
    assert ">${esc(f)}</button>" in t


async def test_history_uses_last_updated_and_clamps_to_window(mkc):
    c, ha, _ = await mkc(opts())
    ha.get_history = lambda *_a: [[
        {"entity_id": "climate.lr", "state": "heat", "last_changed": "2020-01-01T00:00:00+00:00",
         "last_updated": "2020-01-01T00:00:00+00:00", "attributes": {"temperature": 70, "hvac_action": "idle"}},
        {"entity_id": "climate.lr", "state": "heat", "last_changed": "2020-01-01T00:00:00+00:00",
         "last_updated": "2027-01-15T07:30:00+00:00", "attributes": {"temperature": 72, "hvac_action": "heating"}}]]
    j = await (await c.get("/api/zone/lr/history?range=24h")).json()
    assert j["setpoint"][0][0] == j["start"]  # carried-in row clamped, not 2020
    assert j["setpoint"][1] == [1800000000 - 86400 + (j["setpoint"][1][0] - j["start"]), 72]
    assert [h[1] for h in j["heating"]] == [0, 1]


async def test_history_off_is_never_heating(mkc):
    c, ha, _ = await mkc(opts())
    ha.get_history = lambda *_a: [[
        {"entity_id": "climate.bath", "state": "off", "last_updated": "2027-01-15T07:00:00+00:00",
         "attributes": {"temperature": 72, "hvac_action": "heating"}},
        {"entity_id": "climate.bath", "state": "heat", "last_updated": "2027-01-15T07:10:00+00:00",
         "attributes": {"temperature": 72}}]]
    j = await (await c.get("/api/zone/bath/history?range=24h")).json()
    assert [h[1] for h in j["heating"]] == [0, 1]  # off + stale 'heating' -> 0; no hvac_action -> state fallback


async def test_history_same_timestamp_with_none_setpoint_does_not_crash(mkc):
    c, ha, _ = await mkc(opts())
    ha.get_history = lambda *_a: [[
        {"entity_id": "climate.lr", "state": "off", "last_updated": "2020-01-01T00:00:00+00:00", "attributes": {}},
        {"entity_id": "climate.lr", "state": "heat", "last_updated": "2020-01-02T00:00:00+00:00",
         "attributes": {"temperature": 71}}]]
    r = await c.get("/api/zone/lr/history?range=24h")
    assert r.status == 200
    assert [s[1] for s in (await r.json())["setpoint"]] == [None, 71]


def test_health_access_filtered():
    msg = '127.0.0.1 [..] "GET /api/health HTTP/1.1" 200 190 "-" "x"'
    record = logging.LogRecord("aiohttp.access", logging.INFO, "", 0, msg, None, None)
    assert _NoHealthAccess().filter(record) is False


def test_other_access_kept():
    filt = _NoHealthAccess()
    for msg in ('"GET /api/state HTTP/1.1"', '"POST /api/zone/living_room/mode HTTP/1.1"'):
        record = logging.LogRecord("aiohttp.access", logging.INFO, "", 0, msg, None, None)
        assert filt.filter(record) is True


async def test_state_live_reflects_app_write(mkc):
    c, ha, e = await mkc(opts())
    e.st["last_cmd"].clear()  # not a manual change; simulate a fresh decision
    st, at = ha.s["climate.lr"]
    ha.s["climate.lr"] = (st, dict(at, temperature=65))
    e.evaluate()  # lr at 65 -> app sets 71
    j = await (await c.get("/api/state")).json()
    lr = j["last"]["zones"]["lr"]
    assert lr["applied"], lr
    assert lr["live"]["setpoint"] == lr["setpoint"] == ha.s["climate.lr"][1]["temperature"]
