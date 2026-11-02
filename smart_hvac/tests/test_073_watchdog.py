"""0.7.3: Supervisor watchdog on /api/health. Health must never depend on a control cycle or the HA API."""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rootfs", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import yaml  # noqa: E402

from engine import Engine  # noqa: E402
from server import make_app  # noqa: E402
from test_engine import FakeHA, lt14, opts, states  # noqa: E402

CFG = os.path.join(os.path.dirname(__file__), "..", "config.yaml")


def test_watchdog_url_in_config():
    # 0.7.6: the obsolete 'watchdog' key is gone; the Dockerfile HEALTHCHECK probes the same endpoint and port.
    cfg = yaml.safe_load(open(CFG))
    assert "watchdog" not in cfg
    assert "ports" not in cfg and "ingress_port" not in cfg  # 8099 is the default ingress port
    df = open(os.path.join(os.path.dirname(CFG), "Dockerfile")).read()
    assert "HEALTHCHECK" in df and "http://127.0.0.1:8099/api/health" in df and "python3" in df


class BlockingHA(FakeHA):
    """get_states blocks until released: a hung HA API / long control cycle."""
    def __init__(self, s):
        super().__init__(s)
        self.gate = threading.Event()
        self.entered = threading.Event()

    def get_states(self):
        self.entered.set()
        self.gate.wait(10)
        return super().get_states()


class DownHA(FakeHA):
    def get_states(self):
        raise ConnectionError("HA down")

    def call(self, *a):
        raise ConnectionError("HA down")


async def _client(aiohttp_client, monkeypatch, ha):
    monkeypatch.setenv("SH_ALLOW", "172.30.32.2")  # Supervisor/ingress only; test client is 127.0.0.1
    e = Engine(opts(), ha, None, clock=lambda: 1_800_000_000, localtime=lt14)
    return await aiohttp_client(make_app(e, interval=3600))


async def test_health_fast_while_cycle_blocked(aiohttp_client, monkeypatch):
    ha = BlockingHA(states())
    c = await _client(aiohttp_client, monkeypatch, ha)
    try:
        await asyncio.get_running_loop().run_in_executor(None, ha.entered.wait, 5)
        assert ha.entered.is_set()  # first cycle is running and stuck inside the HA API
        t = time.monotonic()
        r = await asyncio.wait_for(c.get("/api/health"), 2)
        assert r.status == 200 and (await r.json()) == {"ok": True}
        assert time.monotonic() - t < 1.0
    finally:
        ha.gate.set()


async def test_health_ok_when_ha_api_down(aiohttp_client, monkeypatch):
    c = await _client(aiohttp_client, monkeypatch, DownHA(states()))
    for _ in range(5):
        r = await c.get("/api/health")
        assert r.status == 200


async def test_health_from_supervisor_ip_allowed(aiohttp_client, monkeypatch):
    # Watchdog requests come from the Supervisor (172.30.32.2); the test client is 127.0.0.1 so allow both.
    monkeypatch.setenv("SH_ALLOW", "172.30.32.2,127.0.0.1")
    e = Engine(opts(), FakeHA(states()), None, clock=lambda: 1_800_000_000, localtime=lt14)
    c = await aiohttp_client(make_app(e, interval=3600))
    assert (await c.get("/api/health")).status == 200
