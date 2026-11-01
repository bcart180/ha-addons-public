"""One-time read-only import of a pyscript inbox. Fake 555 numbers only."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import inbox_store
import legacy_import


def _legacy_row() -> dict:
    return {
        "vm-555-1": {
            "voicemail_id": "vm-555-1",
            "call_id": "CA5550100",
            "caller": "+15555550100",
            "caller_pretty": "+1 (555) 555-0100",
            "caller_title": "Example Office",
            "date": "2026-10-01T15:04:00+00:00",
            "transcript": "Please call 555-555-0101 about the sample.",
            "snippet": "Please call 555-555-0101 about the sample.",
            "heard": True,
            "duration_sec": 9,
            "has_audio": True,
        }
    }


def test_import_is_read_only_and_idempotent(tmp_path):
    source = tmp_path / "legacy" / ".inbound_voicemails.json"
    source.parent.mkdir()
    payload = json.dumps(_legacy_row())
    source.write_text(payload, encoding="utf-8")
    os.chmod(source, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    before = source.read_bytes()
    mode = source.stat().st_mode
    wav = tmp_path / "mirror" / "CA5550100.wav"
    wav.parent.mkdir()
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEsample")
    store = inbox_store.VoicemailStore(tmp_path / "data" / "inbox.json", tmp_path / "audio")
    marker = tmp_path / "data" / "legacy_import.json"
    other = tmp_path / "other.json"
    other.write_text('{"vm-other": {"voicemail_id": "vm-other", "caller": "+15555550199"}}', encoding="utf-8")
    first = legacy_import.import_legacy_once(store, [source, other], marker, [wav.parent])
    assert first["status"] == "imported"
    assert first["count"] == 1
    assert source.read_bytes() == before
    assert source.stat().st_mode == mode
    assert not (source.parent / ".inbound_voicemails.json.tmp").exists()
    item = store.get("vm-555-1")
    assert item["heard"] is True
    assert item["caller"] == "+15555550100"
    assert item["source"] == "legacy_pyscript"
    assert store.read_audio(item) == b"RIFF\x00\x00\x00\x00WAVEsample"
    second = legacy_import.import_legacy_once(store, [source, other], marker, [wav.parent])
    assert second["status"] == "already"
    assert len(store.list_items()) == 1
    names = {path.name for path in source.parent.iterdir()}
    assert names == {".inbound_voicemails.json"}


def test_missing_file_does_not_write_a_marker(tmp_path):
    store = inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")
    marker = tmp_path / "legacy_import.json"
    result = legacy_import.import_legacy_once(
        store,
        [
            tmp_path / "homeassistant" / "pyscript" / ".inbound_voicemails.json",
            tmp_path / "config" / "pyscript" / ".inbound_voicemails.json",
        ],
        marker,
    )
    assert result["status"] == "absent"
    assert not marker.exists()
    assert store.list_items() == []


def test_list_shape_and_retry_without_marker_does_not_duplicate(tmp_path):
    source = tmp_path / "rows.json"
    source.write_text(
        json.dumps(
            [
                {
                    "voicemail_id": "vm-555-2",
                    "caller": "+15555550199",
                    "transcript": "Sample 555.",
                    "date": "2026-10-02T00:00:00+00:00",
                }
            ]
        ),
        encoding="utf-8",
    )
    store = inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")
    marker = tmp_path / "marker.json"
    legacy_import.import_legacy_once(store, [source], marker)
    marker.unlink()
    again = legacy_import.import_legacy_once(store, [source], marker)
    assert again["count"] == 1
    assert len(store.list_items()) == 1


def test_candidates_prefer_the_homeassistant_path(tmp_path):
    home = tmp_path / "home.json"
    config = tmp_path / "config.json"
    home.write_text(json.dumps(_legacy_row()), encoding="utf-8")
    config.write_text(
        json.dumps({"vm-555-9": {"voicemail_id": "vm-555-9", "caller": "+15555550198", "transcript": "nope"}}),
        encoding="utf-8",
    )
    store = inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")
    legacy_import.import_legacy_once(store, [home, config], tmp_path / "marker.json")
    assert store.get("vm-555-1") is not None
    assert store.get("vm-555-9") is None


def test_default_candidate_names():
    paths = [str(path) for path in legacy_import.DEFAULT_CANDIDATES]
    assert "/homeassistant/pyscript/.inbound_voicemails.json" in paths
    assert "/config/pyscript/.inbound_voicemails.json" in paths
