"""0.8.0 heater API + client allowlist; the split engine is untouched."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))
import pytest  # noqa: E402

import ha_client  # noqa: E402
from engine import Engine  # noqa: E402
from server import make_app  # noqa: E402
from test_080_spare_heater import mk  # noqa: E402
from test_engine import FakeHA, lt14, opts, states  # noqa: E402


@pytest.fixture
def mkc(aiohttp_client, monkeypatch):
    async def _mk():
        monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
        e = Engine(opts(), FakeHA(states()), None, clock=lambda: 1_800_000_000, localtime=lt14)
        h, hha, _c = mk(temp=60)
        hha.sw = "on"
        return await aiohttp_client(make_app(e, interval=3600, heater=h, heater_tick=3600)), h, hha, e
    return _mk


async def test_startup_off_then_api(mkc):
    c, h, hha, e = await mkc()
    assert hha.cmds[0] is False                       # startup safe-off first
    j = await (await c.get("/api/state")).json()
    before = list(e.ha.calls)                         # the split engine's own startup cycle
    assert j["heater"]["entity"] == "climate.spare_bathroom" and j["heater"]["setpoint"] == 68
    r = await c.post("/api/heater/spare_bathroom/setpoint", json={"temperature": 71})
    assert (await r.json())["heater"]["setpoint"] == 71
    r = await c.post("/api/heater/spare_bathroom/mode", json={"hvac_mode": "off"})
    assert (await r.json())["heater"]["mode"] == "off" and hha.sw == "off"
    assert (await c.post("/api/heater/spare_bathroom/mode", json={"hvac_mode": "cool"})).status == 400
    assert (await c.post("/api/heater/spare_bathroom/setpoint", json={"temperature": True})).status == 400
    assert (await c.post("/api/heater/other/mode", json={"hvac_mode": "off"})).status == 404
    assert e.ha.calls == before                         # no split write from heater paths


async def test_shutdown_turns_plug_off(aiohttp_client, monkeypatch):
    monkeypatch.setenv("SH_ALLOW", "127.0.0.1")
    e = Engine(opts(), FakeHA(states()), None, clock=lambda: 1_800_000_000, localtime=lt14)
    h, hha, _ = mk(temp=60)
    c = await aiohttp_client(make_app(e, interval=3600, heater=h, heater_tick=3600))
    await c.post("/api/heater/spare_bathroom/mode", json={"hvac_mode": "heat"})
    assert hha.sw == "on"
    await c.close()
    assert hha.sw == "off" and h.last["reason"] == "shutdown"


def test_client_allowlist(monkeypatch):
    cl = ha_client.HAClient(token="t", base="http://x")
    sent = []
    monkeypatch.setattr(cl, "_req", lambda m, p, b=None: sent.append((p, b)))
    with pytest.raises(PermissionError):
        cl.heater_switch("climate.room_a", True)
    with pytest.raises(PermissionError):
        cl.heater_switch("light.x", True)
    with pytest.raises(PermissionError):
        cl.publish_state("climate.room_a", "heat", {})
    cl.heater_switch("switch.fake_plug_555", False)
    cl.heater_notify("t", "m", "script.bad")
    assert [p for p, _ in sent] == ["/services/switch/turn_off", "/services/persistent_notification/create"]
