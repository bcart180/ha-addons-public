"""Unit tests for M1/M2 trust helpers (no live HA)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))


def _load_server(monkeypatch, media_player=""):
    # Avoid binding real options.json; stub env before import.
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("VM_OPTIONS", "/nonexistent/options.json")
    monkeypatch.setenv("VM_MOCK", "1")
    monkeypatch.setenv("VM_DATA_DIR", tmp)
    monkeypatch.setenv("VM_AUDIO_ROOT", str(pathlib.Path(tmp) / "audio"))
    monkeypatch.setenv("VM_LEGACY_PATHS", str(pathlib.Path(tmp) / "missing.json"))
    # Fresh load
    for name in list(sys.modules):
        if name == "server" or name.startswith("server."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    # Patch MEDIA_PLAYER after load by rewriting OPTS via env is hard; load then override.
    spec.loader.exec_module(mod)
    mod.MEDIA_PLAYER = media_player
    mod.ALLOW = ["172.30.0.0/16"]
    mod.SUPERVISOR_INGRESS_IP = "172.30.32.2"
    return mod


class _Req:
    def __init__(self, remote, headers=None, body=None):
        self.remote = remote
        self.headers = headers or {}
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_mutation_rejects_sibling_lan(monkeypatch):
    mod = _load_server(monkeypatch)
    req = _Req("172.30.33.99", headers={"X-Remote-User-Id": "abc"})
    resp = mod.deny_mutation(req)
    assert resp is not None
    assert resp.status == 403


@pytest.mark.asyncio
async def test_mutation_rejects_ingress_without_user(monkeypatch):
    mod = _load_server(monkeypatch)
    req = _Req("172.30.32.2", headers={})
    resp = mod.deny_mutation(req)
    assert resp is not None
    assert resp.status == 403


@pytest.mark.asyncio
async def test_mutation_allows_ingress_user(monkeypatch):
    mod = _load_server(monkeypatch)
    req = _Req("172.30.32.2", headers={"X-Remote-User-Id": "user-1"})
    assert mod.deny_mutation(req) is None


@pytest.mark.asyncio
async def test_play_rejects_media_player_override(monkeypatch):
    mod = _load_server(monkeypatch, media_player="media_player.kitchen_speaker")
    # Bypass mutation gate by making deny_mutation return None and stub ha_call_service
    monkeypatch.setattr(mod, "deny_mutation", lambda r: None)
    monkeypatch.setattr(mod, "audio_file_for", lambda cid: None)

    async def fake_ha(*a, **k):
        return 200, {"ok": True}

    monkeypatch.setattr(mod, "ha_call_service", fake_ha)
    app = {"session": object()}
    req = _Req(
        "172.30.32.2",
        headers={"X-Remote-User-Id": "u"},
        body={"call_id": "CA123", "media_player": "media_player.evil"},
    )
    # api_play expects request.app
    req.app = app  # type: ignore
    resp = await mod.api_play(req)
    assert resp.status == 400
    # aiohttp json response body
    body = resp.body
    assert b"media_player-not-overridable" in body


@pytest.mark.asyncio
async def test_play_without_media_player_does_not_call_home_assistant(monkeypatch):
    mod = _load_server(monkeypatch)
    assert mod.MEDIA_PLAYER == ""
    called = {"n": 0}

    async def fake_ha(*_a, **_k):
        called["n"] += 1
        return 200, {"ok": True}

    monkeypatch.setattr(mod, "ha_call_service", fake_ha)
    monkeypatch.setattr(mod, "deny_mutation", lambda _r: None)
    req = _Req("172.30.32.2", headers={"X-Remote-User-Id": "u"}, body={"call_id": "CA5550100"})
    req.app = {}
    resp = await mod.api_play(req)
    assert resp.status == 400
    assert b"media_player-not-configured" in resp.body
    assert called["n"] == 0
    assert "bedroom_homepod" not in pathlib.Path(mod.__file__).read_text(encoding="utf-8")
