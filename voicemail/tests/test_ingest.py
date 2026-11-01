"""Ingest auth and the legacy-vs-store switch. Fake 555 numbers only."""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))


def _load(monkeypatch, tmp_path, options, legacy: pathlib.Path | None = None):
    opt = tmp_path / "options.json"
    opt.parent.mkdir(parents=True, exist_ok=True)
    opt.write_text(json.dumps(options), encoding="utf-8")
    monkeypatch.setenv("VM_OPTIONS", str(opt))
    monkeypatch.setenv("VM_MOCK", "1")
    monkeypatch.setenv("VM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VM_AUDIO_ROOT", str(tmp_path / "audio"))
    monkeypatch.setenv("VM_LEGACY_PATHS", str(legacy or (tmp_path / "missing-legacy.json")))
    monkeypatch.setenv("VM_MIRROR_DIRS", str(tmp_path / "mirror"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    for name in list(sys.modules):
        if name == "server" or name.startswith("server."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server"] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _options(**extra):
    base = {
        "mock": True,
        "ingest_token": "ingest-token-555",
        "legacy_pyscript": "off",
        "storage_backend": "local",
        "media_player": "",
        "sensor_entity": "sensor.inbound_voicemails",
        "log_level": "warning",
    }
    base.update(extra)
    return base


class _Req:
    def __init__(self, body):
        self.remote = "172.30.32.2"
        self.headers = {"X-Remote-User-Id": "user-555"}
        self._body = body
        self.app = {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_ingest_requires_bearer_and_stores_audio(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path, _options())
    client = TestClient(TestServer(mod.make_app()))
    await client.start_server()
    try:
        early = await client.post(
            "/api/ingest",
            data=b"not-json{{{",
            headers={"Content-Type": "application/json"},
        )
        assert early.status == 401
        body_token = await client.post(
            "/api/ingest",
            json={
                "call_id": "CA5550198",
                "from": "+15555550198",
                "transcript_text": "body token is not a credential",
                "token": "ingest-token-555",
            },
        )
        assert body_token.status == 401
        webhook_only = await client.post(
            "/api/ingest",
            json={"call_id": "CA5550197", "from": "+15555550197", "transcript_text": "header only"},
            headers={"X-Webhook-Token": "ingest-token-555"},
        )
        assert webhook_only.status == 401
        missing = await client.post(
            "/api/ingest",
            json={"call_id": "CA5550100", "from": "+15555550100", "transcript_text": "Hi"},
        )
        assert missing.status == 401
        wrong = await client.post(
            "/api/ingest",
            json={"call_id": "CA5550100", "from": "+15555550100", "transcript_text": "Hi"},
            headers={"Authorization": "Bearer other-token-555"},
        )
        assert wrong.status == 401
        wav = b"RIFF\x00\x00\x00\x00WAVEsample-555"
        ok = await client.post(
            "/api/ingest",
            json={
                "call_id": "CA5550100",
                "from": "+15555550100",
                "ended_at": "2026-10-29T14:00:00+00:00",
                "duration_s": 12.9,
                "transcript_text": "Please call 555-555-0101.",
                "token": "ingest-token-555",
                "source": "attacker",
                "drive_audio_id": "evil-drive-file",
                "drive_transcript_id": "evil-json-file",
                "drive_status": "uploaded",
                "audio_base64": base64.b64encode(wav).decode("ascii"),
            },
            headers={"Authorization": "Bearer ingest-token-555"},
        )
        assert ok.status == 200
        bad = await client.post(
            "/api/ingest",
            data=b"not-json{{{",
            headers={"Authorization": "Bearer ingest-token-555", "Content-Type": "application/json"},
        )
        assert bad.status == 400
        bad_body = await bad.json()
        assert bad_body == {"ok": False, "error": "bad-request"}
        body = await ok.json()
        assert body["has_audio"] is True
        assert body["voicemail_id"] == "CA5550100"
        closed = await client.get("/api/voicemails")
        assert closed.status == 403
        health = await client.get("/api/health")
        assert health.status == 200
        auth = {"Authorization": "Bearer ingest-token-555"}
        listed = await (await client.get("/api/voicemails", headers=auth)).json()
        assert listed["count"] == 1
        assert listed["legacy_pyscript"] is False
        assert listed["media_player"] == ""
        assert listed["voicemails"][0]["caller_pretty"] == "+1 (555) 555-0100"
        assert listed["voicemails"][0]["duration_sec"] == 12
        raw = (tmp_path / "data" / "inbox.json").read_text(encoding="utf-8")
        assert "ingest-token-555" not in raw
        assert "audio_base64" not in raw
        assert "evil-drive-file" not in raw
        saved_row = client.server.app[mod.RT]["store"].get("CA5550100")
        assert saved_row["source"] == "ingest"
        assert saved_row["drive_audio_id"] == ""
        assert saved_row["drive_status"] == "local"
        diag = await (await client.get("/api/diag", headers=auth)).json()
        assert diag["ingest_configured"] is True
        assert diag["storage_backend"] == "local"
        assert "ingest-token-555" not in json.dumps(diag)
        client.server.app[mod.RT]["store"].mark_heard(voicemail_id="CA5550100")
        again = await client.post(
            "/api/ingest",
            json={
                "call_id": "CA5550100",
                "from": "+15555550100",
                "transcript_text": "Updated 555-555-0101.",
                "token": "ingest-token-555",
            },
            headers={"Authorization": "Bearer ingest-token-555"},
        )
        assert again.status == 200
        saved = client.server.app[mod.RT]["store"].get("CA5550100")
        assert saved["heard"] is True
        assert "Updated" in saved["transcript"]
        assert len(client.server.app[mod.RT]["store"].list_items()) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingest_multipart_and_disabled_token(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path, _options())
    client = TestClient(TestServer(mod.make_app()))
    await client.start_server()
    try:
        from aiohttp import FormData

        form = FormData()
        form.add_field(
            "metadata",
            json.dumps(
                {
                    "call_id": "CA5550102",
                    "from": "+15555550102",
                    "transcript_text": "Multipart sample 555.",
                }
            ),
            content_type="application/json",
        )
        form.add_field(
            "audio",
            b"RIFF\x00\x00\x00\x00WAVEmp3-555",
            filename="CA5550102.wav",
            content_type="audio/wav",
        )
        resp = await client.post("/api/ingest", data=form, headers={"Authorization": "Bearer ingest-token-555"})
        assert resp.status == 200
        body = await resp.json()
        assert body["has_audio"] is True
    finally:
        await client.close()

    disabled = _load(monkeypatch, tmp_path / "off", _options(ingest_token=""))
    client = TestClient(TestServer(disabled.make_app()))
    await client.start_server()
    try:
        resp = await client.post(
            "/api/ingest",
            json={"call_id": "CA5550103", "from": "+15555550103", "transcript_text": "no"},
            headers={"Authorization": "Bearer ingest-token-555"},
        )
        assert resp.status == 503
        assert (await resp.json())["error"] == "ingest-disabled"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_clear_uses_pyscript_and_off_uses_the_store(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(
        json.dumps(
            {
                "vm-555-1": {
                    "voicemail_id": "vm-555-1",
                    "call_id": "CA5550100",
                    "caller": "+15555550100",
                    "transcript": "Legacy sample.",
                    "date": "2026-10-01T00:00:00+00:00",
                    "heard": False,
                }
            }
        ),
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    mod = _load(monkeypatch, tmp_path, _options(legacy_pyscript="auto"), legacy)
    assert mod.legacy_enabled() is True
    called = {}

    async def fake_ha(_session, domain, service, data=None):
        called["domain"] = domain
        called["service"] = service
        called["data"] = data
        return 200, {"ok": True}

    monkeypatch.setattr(mod, "ha_call_service", fake_ha)
    monkeypatch.setattr(mod, "deny_mutation", lambda _r: None)
    req = _Req({"voicemail_id": "vm-555-1"})
    req.app = {mod.RT: {"session": object()}}
    resp = await mod.api_clear_one(req)
    assert resp.status == 200
    assert called["domain"] == "pyscript"
    assert called["service"] == "inbound_voicemail_clear_one"
    assert legacy.read_bytes() == before

    own = _load(monkeypatch, tmp_path / "own", _options(legacy_pyscript="off"), legacy)
    assert own.legacy_enabled() is False
    calls = {"n": 0}

    async def boom(*_a, **_k):
        calls["n"] += 1
        return 200, {}

    monkeypatch.setattr(own, "ha_call_service", boom)
    monkeypatch.setattr(own, "deny_mutation", lambda _r: None)
    req = _Req({"voicemail_id": "vm-555-1"})
    resp = await own.api_clear_one(req)
    assert resp.status == 200
    assert calls["n"] == 0
    assert req.app[own.RT]["store"].get("vm-555-1") is None
    assert legacy.read_bytes() == before
    missing = _Req({"voicemail_id": "vm-555-1"})
    missing.app = req.app
    resp = await own.api_clear_one(missing)
    assert resp.status == 404


def test_legacy_auto_does_not_import_or_write_marker(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    payload = {
        "vm-555-1": {
            "voicemail_id": "vm-555-1",
            "call_id": "CA5550100",
            "caller": "+15555550100",
            "transcript": "Legacy sample 555.",
            "date": "2026-10-01T00:00:00+00:00",
            "source": "attacker",
            "drive_audio_id": "evil-drive-file",
        }
    }
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    before = legacy.read_bytes()
    auto = _load(monkeypatch, tmp_path / "auto", _options(legacy_pyscript="auto"), legacy)
    app = {auto.RT: {}}
    auto.ensure_runtime(app)
    assert not (tmp_path / "auto" / "data" / "legacy_import.json").exists()
    assert app[auto.RT]["migration"]["status"] == "deferred"
    assert app[auto.RT]["store"].list_items() == []
    assert legacy.read_bytes() == before

    off = _load(monkeypatch, tmp_path / "off", _options(legacy_pyscript="off"), legacy)
    app2 = {off.RT: {}}
    off.ensure_runtime(app2)
    marker = tmp_path / "off" / "data" / "legacy_import.json"
    assert marker.is_file()
    imported = app2[off.RT]["store"].get("vm-555-1")
    assert imported["source"] == "legacy_pyscript"
    assert imported["drive_audio_id"] == ""
    assert "evil-drive-file" not in marker.read_text(encoding="utf-8")
    off.ensure_runtime(app2)
    assert len(app2[off.RT]["store"].list_items()) == 1
    assert legacy.read_bytes() == before


def test_lan_read_needs_ingress_session_or_bearer(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path, _options())
    mod.ALLOW = ["172.30.0.0/16"]
    mod.SUPERVISOR_INGRESS_IP = "172.30.32.2"

    class Req:
        def __init__(self, remote, headers):
            self.remote = remote
            self.headers = headers

    assert mod.deny_sensitive_read(Req("172.30.33.50", {})).status == 403
    assert mod.deny_sensitive_read(Req("172.30.33.50", {"X-Webhook-Token": "ingest-token-555"})).status == 403
    assert mod.deny_sensitive_read(Req("172.30.33.50", {"Authorization": "Bearer ingest-token-555"})) is None
    assert mod.deny_sensitive_read(Req("172.30.32.2", {"X-Remote-User-Id": "user-555"})) is None
    assert mod.deny_sensitive_read(Req("10.1.1.5", {"Authorization": "Bearer ingest-token-555"})).status == 403
