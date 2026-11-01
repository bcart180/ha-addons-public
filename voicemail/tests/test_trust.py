"""0.3.0 trust gate: list proxy to CallCatch Voice, STIR fields. Fake 555 numbers only."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import inbox_store  # noqa: E402

TOKEN = "voice-trust-token-555-abcdef"


def _load(monkeypatch, tmp_path, **extra):
    options = {
        "mock": True,
        "ingest_token": "ingest-token-555",
        "legacy_pyscript": "off",
        "storage_backend": "local",
        "log_level": "warning",
    }
    options.update(extra)
    opt = tmp_path / "options.json"
    opt.write_text(json.dumps(options), encoding="utf-8")
    monkeypatch.setenv("VM_OPTIONS", str(opt))
    monkeypatch.setenv("VM_MOCK", "1")
    monkeypatch.setenv("VM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VM_AUDIO_ROOT", str(tmp_path / "audio"))
    monkeypatch.setenv("VM_LEGACY_PATHS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("VM_MIRROR_DIRS", str(tmp_path / "mirror"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    for name in list(sys.modules):
        if name == "server":
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server"] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_voice_trust_url_only_internal_hostnames():
    spec = importlib.util.spec_from_file_location("server_url", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    ok = mod.voice_trust_base
    assert ok("http://47b3c4e5-twilio-house-voice:8787") == "http://47b3c4e5-twilio-house-voice:8787"
    assert ok("http://local-twilio-house-voice:8787/") == "http://local-twilio-house-voice:8787"
    for bad in (
        "",
        "https://47b3c4e5-twilio-house-voice:8787",
        "http://voice.example.com:8787",
        "http://10.0.1.5:8787",
        "http://user@host:8787",
        "http://host:8787/api/trust",
        "http://host",
    ):
        assert ok(bad) == "", bad


def test_item_keeps_sanitized_stir_and_verdict():
    item = inbox_store.item_from_payload({
        "call_id": "CA5550100",
        "from": "+15555550100",
        "stir_verstat": "TN-Validation-Passed-A",
        "trust_verdict": "allow",
    })
    assert item["stir_verstat"] == "TN-Validation-Passed-A"
    assert item["trust_verdict"] == "allow"
    bad = inbox_store.item_from_payload({
        "call_id": "CA5550101", "stir_verstat": "<img src=x>", "trust_verdict": "block",
    })
    assert bad["stir_verstat"] == "" and bad["trust_verdict"] == ""
    assert inbox_store.stir_summary("TN-Validation-Failed-B") == {"status": "failed", "level": "B"}
    assert inbox_store.stir_summary("No-TN-Validation")["status"] == "none"
    assert inbox_store.stir_summary(None)["status"] == "absent"
    state = inbox_store.sensor_state([item])
    assert state["attributes"]["recent_items"][0]["stir_status"] == "passed"


@pytest.mark.asyncio
async def test_trust_routes_proxy_with_bearer_and_ingress_gate(monkeypatch, tmp_path):
    seen = []

    async def fake_get(request):
        seen.append(("GET", request.headers.get("Authorization"), None))
        return web.json_response({"ok": True, "allow": [], "block": [{"number": "+15555550100"}]})

    async def fake_add(request):
        body = await request.json()
        seen.append(("add", request.headers.get("Authorization"), body))
        if body["number"] == "bad":
            return web.json_response({"ok": False, "error": "bad-number"}, status=400)
        return web.json_response({"ok": True, "entry": {"number": "+15555550100"}, "previous": "allow"})

    async def fake_remove(request):
        body = await request.json()
        seen.append(("remove", request.headers.get("Authorization"), body))
        return web.json_response({"ok": True, "removed": True})

    voice = web.Application()
    voice.router.add_get("/api/trust", fake_get)
    voice.router.add_post("/api/trust/add", fake_add)
    voice.router.add_post("/api/trust/remove", fake_remove)
    async with TestServer(voice) as vs:
        mod = _load(monkeypatch, tmp_path, voice_trust_token=TOKEN)
        # Test server listens on 127.0.0.1; production only accepts add-on hostnames.
        mod.VOICE_TRUST_BASE = str(vs.make_url("")).rstrip("/")
        mod.SUPERVISOR_INGRESS_IP = "127.0.0.1"
        async with TestClient(TestServer(mod.make_app())) as client:
            user = {"X-Remote-User-Id": "user-555"}
            # No HA user header: refused, nothing forwarded.
            assert (await client.get("/api/trust")).status == 403
            assert (await client.post("/api/trust/add", json={"list": "block", "number": "+15555550100"})).status == 403
            # The ingest bearer is not enough to edit lists.
            r = await client.post("/api/trust/add", json={"list": "block", "number": "+15555550100"},
                                  headers={"Authorization": "Bearer ingest-token-555"})
            assert r.status == 403
            assert seen == []
            got = await client.get("/api/trust", headers=user)
            assert got.status == 200
            assert (await got.json())["block"][0]["number"] == "+15555550100"
            add = await client.post("/api/trust/add", json={"list": "block", "number": "+1 555 555 0100"}, headers=user)
            assert add.status == 200
            assert (await add.json())["previous"] == "allow"
            rm = await client.post("/api/trust/remove", json={"list": "block", "number": "+15555550100"}, headers=user)
            assert (await rm.json())["removed"] is True
            assert (await client.post("/api/trust/add", json={"list": "vip", "number": "1"}, headers=user)).status == 400
            assert (await client.post("/api/trust/add", data="nope", headers=user)).status == 400
            assert all(auth == f"Bearer {TOKEN}" for _, auth, _ in seen)
            assert seen[1][2] == {"list": "block", "number": "+1 555 555 0100", "note": ""}


@pytest.mark.asyncio
async def test_trust_not_configured_and_unreachable(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    mod.SUPERVISOR_INGRESS_IP = "127.0.0.1"
    user = {"X-Remote-User-Id": "user-555"}
    async with TestClient(TestServer(mod.make_app())) as client:
        r = await client.get("/api/trust", headers=user)
        assert r.status == 503
        assert (await r.json())["error"] == "trust-not-configured"
    mod = _load(monkeypatch, tmp_path, voice_trust_token=TOKEN)
    mod.SUPERVISOR_INGRESS_IP = "127.0.0.1"
    mod.VOICE_TRUST_BASE = "http://127.0.0.1:9"
    async with TestClient(TestServer(mod.make_app())) as client:
        r = await client.get("/api/trust", headers=user)
        assert r.status == 502
        body = await r.text()
        assert TOKEN not in body


def test_token_is_a_password_and_redacted(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path, voice_trust_token=TOKEN)
    assert TOKEN not in mod.redact(f"x {TOKEN} y")
    config = (ROOT.parents[1] / "config.yaml").read_text(encoding="utf-8")
    assert "voice_trust_token: password" in config
    assert "voice_trust_url: str?" in config
