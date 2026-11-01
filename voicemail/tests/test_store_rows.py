"""Legacy panel reads full rows from the pyscript store file. Fake 555 numbers only."""
from __future__ import annotations

import json

import pytest
from test_ingest import _load, _options, _Req

FULL = {
    "vm-555-1": {
        "voicemail_id": "vm-555-1",
        "call_id": "CA5550100",
        "caller": "+15555550100",
        "caller_pretty": "(555) 555-0100",
        "transcript": "Full legacy transcript.",
        "snippet": "Full legacy",
        "date": "2026-10-01T00:00:00+00:00",
        "heard": False,
    }
}
REDACTED = {
    "state": "1",
    "attributes": {
        "unheard": 0,
        "voicemails": [
            {
                "voicemail_id": "vm-555-1",
                "call_id": "CA5550100",
                "caller": "\u2022\u2022\u2022\u20220100",
                "caller_pretty": "\u2022\u2022\u2022\u20220100",
                "caller_title": "\u2022\u2022\u2022\u20220100",
                "heard": True,
                "redacted": True,
            }
        ],
    },
}


async def _list(mod, monkeypatch, sensor):
    async def fake_state(_session, _entity):
        return sensor

    monkeypatch.setattr(mod, "ha_get_state", fake_state)
    monkeypatch.setattr(mod, "deny_sensitive_read", lambda _r: None)
    req = _Req({})
    req.app = {mod.RT: {"session": object()}}
    resp = await mod.api_voicemails(req)
    assert resp.status == 200
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_redacted_sensor_gets_full_rows_from_store(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(FULL), encoding="utf-8")
    before = legacy.read_bytes()
    mod = _load(monkeypatch, tmp_path, _options(legacy_pyscript="auto"), legacy)
    body = await _list(mod, monkeypatch, REDACTED)
    row = body["voicemails"][0]
    assert body["rows_from"] == "store"
    assert row["transcript"] == "Full legacy transcript."
    assert row["tel"] and "5555550100" in row["tel"]
    assert row["caller_digits"].endswith("5555550100")
    assert "\u2022" not in row["caller_title"]
    assert row["heard"] is True  # sensor heard state kept
    assert legacy.read_bytes() == before


@pytest.mark.asyncio
async def test_unreadable_store_falls_back_to_sensor(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("not-json{{{", encoding="utf-8")
    mod = _load(monkeypatch, tmp_path, _options(legacy_pyscript="auto"), legacy)
    body = await _list(mod, monkeypatch, REDACTED)
    assert body["rows_from"] == "sensor"
    assert body["count"] == 1
    assert body["voicemails"][0]["caller_pretty"].endswith("0100")


@pytest.mark.asyncio
async def test_sensor_row_missing_from_store_is_kept(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({}), encoding="utf-8")
    mod = _load(monkeypatch, tmp_path, _options(legacy_pyscript="auto"), legacy)
    body = await _list(mod, monkeypatch, REDACTED)
    assert body["count"] == 1
    assert body["voicemails"][0]["call_id"] == "CA5550100"


@pytest.mark.asyncio
async def test_public_vm_id_joins_by_short_id(tmp_path, monkeypatch):
    seed_id = "seed-20260823-1143-5555550199"
    full = {seed_id: {"caller": "+15555550199", "transcript": "Seed fake.", "heard": False}}
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps(full), encoding="utf-8")
    mod = _load(monkeypatch, tmp_path, _options(legacy_pyscript="auto"), legacy)
    public = "vm-" + mod.pyscript_short_id(seed_id)
    sensor = {"state": "1", "attributes": {"voicemails": [
        {"voicemail_id": public, "caller": "\u2022\u2022\u2022\u20220199", "redacted": True}]}}
    body = await _list(mod, monkeypatch, sensor)
    row = body["voicemails"][0]
    assert row["voicemail_id"] == seed_id
    assert row["transcript"] == "Seed fake."
    assert row["caller_digits"].endswith("5555550199")


def test_short_id_matches_pyscript_fnv(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path, _options())
    # Known FNV-1a 64 of "a" is AF63DC4C8601EC8C; pyscript keeps the last 10 hex chars.
    assert mod.pyscript_short_id("a") == "4C8601EC8C"
