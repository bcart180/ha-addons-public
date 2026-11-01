"""Store locking, atomic writes, and the sensor payload. Fake 555 numbers only."""
from __future__ import annotations

import pathlib
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import inbox_store


def _store(tmp_path: pathlib.Path) -> inbox_store.VoicemailStore:
    return inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")


def _item(vid: str, date: str, heard: bool = False) -> dict:
    return inbox_store.item_from_payload(
        {
            "voicemail_id": vid,
            "call_id": vid,
            "from": "+15555550100",
            "date": date,
            "transcript_text": "Please call 555-555-0101.",
            "heard": heard,
            "duration_sec": 5,
            "source": "test",
        }
    )


def test_upsert_orders_newest_first_and_rewrites_in_place(tmp_path):
    store = _store(tmp_path)
    store.upsert(_item("CA5550100", "2026-10-01T00:00:00+00:00"))
    store.upsert(_item("CA5550101", "2026-10-03T00:00:00+00:00"))
    store.upsert(_item("CA5550100", "2026-10-01T00:00:00+00:00"), preserve_heard=True)
    rows = store.list_items()
    assert [row["voicemail_id"] for row in rows] == ["CA5550101", "CA5550100"]
    assert rows[1]["caller_pretty"] == "+1 (555) 555-0100"


def test_audio_round_trip_and_clear(tmp_path):
    store = _store(tmp_path)
    wav = b"RIFF\x00\x00\x00\x00WAVEsample-555"
    saved = store.upsert(_item("CA5550100", "2026-10-02T00:00:00+00:00"), audio=wav, ext="wav")
    assert saved["has_audio"] is True
    assert store.read_audio(saved) == wav
    path = store.audio_file(saved)
    assert path is not None and path.is_file()
    removed = store.clear_one("CA5550100")
    assert removed is not None
    assert store.get("CA5550100") is None
    assert not path.exists()
    assert store.clear_one("CA5550100") is None


def test_clear_all_and_mark_heard_by_short_id(tmp_path):
    store = _store(tmp_path)
    item = _item("CA5550102", "2026-10-04T00:00:00+00:00")
    item["short_id"] = "s555"
    store.upsert(item, audio=b"RIFF\x00\x00\x00\x00WAVEok", ext="wav")
    store.upsert(_item("CA5550103", "2026-10-05T00:00:00+00:00"))
    assert store.mark_heard(short_id="s555") is True
    assert store.get("CA5550102")["heard"] is True
    removed = store.clear_all()
    assert len(removed) == 2
    assert store.list_items() == []
    assert list((tmp_path / "audio").glob("*")) == []


def test_ingest_retry_keeps_heard(tmp_path):
    store = _store(tmp_path)
    store.upsert(_item("CA5550100", "2026-10-01T00:00:00+00:00", heard=True))
    again = _item("CA5550100", "2026-10-01T00:00:00+00:00", heard=False)
    again["transcript"] = "Updated 555-555-0101."
    saved = store.upsert(again, preserve_heard=True)
    assert saved["heard"] is True
    assert store.get("CA5550100")["transcript"] == "Updated 555-555-0101."


def test_atomic_write_keeps_previous_bytes_when_replace_fails(tmp_path):
    path = tmp_path / "inbox.json"
    inbox_store.atomic_write_bytes(path, b'{"ok":true}')

    def fail(_tmp):
        raise RuntimeError("disk")

    with pytest.raises(RuntimeError):
        inbox_store.atomic_write_bytes(path, b'{"ok":false}', before_replace=fail)
    assert path.read_bytes() == b'{"ok":true}'
    assert [p.name for p in tmp_path.iterdir()] == ["inbox.json"]


def test_lock_blocks_second_writer(tmp_path):
    store = _store(tmp_path)
    started = threading.Event()
    release = threading.Event()
    acquired = threading.Event()

    def hold():
        with inbox_store.exclusive_lock(store.lock_path):
            started.set()
            assert release.wait(2)

    def waiter():
        with inbox_store.exclusive_lock(store.lock_path):
            acquired.set()

    first = threading.Thread(target=hold)
    second = threading.Thread(target=waiter)
    first.start()
    assert started.wait(2)
    second.start()
    assert acquired.wait(0.2) is False
    release.set()
    second.join(2)
    first.join(2)
    assert acquired.is_set()


def test_corrupt_file_is_kept_aside(tmp_path):
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.list_items() == []
    assert store.path.with_name("inbox.json.corrupt").is_file()
    store.upsert(_item("CA5550100", "2026-10-01T00:00:00+00:00"))
    assert len(store.list_items()) == 1


def test_audio_name_cannot_escape_the_directory(tmp_path):
    store = _store(tmp_path)
    item = _item("CA5550100", "2026-10-01T00:00:00+00:00")
    saved = store.upsert(item, audio=b"RIFF\x00\x00\x00\x00WAVEok", ext="wav")
    saved["audio_name"] = "../outside.wav"
    assert store.read_audio(saved) is None
    outside = tmp_path / "outside.wav"
    assert not outside.exists()


def test_sensor_state_count_and_recent_window():
    items = []
    for n in range(25):
        items.append(
            inbox_store.item_from_payload(
                {
                    "voicemail_id": f"CA555{n:04d}",
                    "from": "+15555550199",
                    "date": f"2026-10-{(n % 28) + 1:02d}T00:00:00+00:00",
                    "transcript_text": "555 " + ("word " * 80),
                    "duration_sec": 3,
                }
            )
        )
    payload = inbox_store.sensor_state(items)
    assert payload["state"] == "25"
    assert payload["attributes"]["total"] == 25
    assert len(payload["attributes"]["recent_items"]) == inbox_store.RECENT_LIMIT
    assert payload["attributes"]["voicemails"] == payload["attributes"]["recent_items"]
    assert payload["attributes"]["unheard"] == 25
    row = payload["attributes"]["recent_items"][0]
    for raw in ("caller", "caller_pretty", "caller_title", "snippet", "transcript"):
        assert raw not in row  # sensor publishes masked fields only
    assert row["caller_masked"] == "•••0199"


def test_voice_payload_and_legacy_mode_switch():
    item = inbox_store.item_from_payload(
        {
            "call_id": "CA5550100",
            "from": "+15555550100",
            "ended_at": "2026-10-29T14:05:00+00:00",
            "duration_s": 12.9,
            "transcript": [
                {"role": "user", "text": "Please call 555-555-0101."},
                {"role": "assistant", "text": "Noted."},
            ],
            "token": "ingest-token-555",
            "audio_base64": "AAAA",
            "source": "attacker",
            "drive_audio_id": "evil-drive-file",
            "drive_transcript_id": "evil-json-file",
            "drive_status": "uploaded",
        }
    )
    assert item["voicemail_id"] == "CA5550100"
    assert item["duration_sec"] == 12
    assert "Caller:" in item["transcript"]
    assert "Assistant: Noted." in item["transcript"]
    assert "CallCatch:" not in item["transcript"]
    assert item["source"] == ""
    assert item["drive_audio_id"] == ""
    assert item["drive_transcript_id"] == ""
    assert item["drive_status"] == ""
    assert "token" not in item
    assert "audio_base64" not in item
    assert inbox_store.legacy_mode("auto", True) is True
    assert inbox_store.legacy_mode("auto", False) is False
    assert inbox_store.legacy_mode("off", True) is False
    assert inbox_store.legacy_mode("on", False) is True


def test_secrets_are_not_part_of_the_stored_shape(tmp_path):
    store = _store(tmp_path)
    store.upsert(
        inbox_store.item_from_payload(
            {
                "call_id": "CA5550199",
                "from": "+15555550199",
                "transcript_text": "Sample only.",
                "token": "ingest-token-555",
            }
        )
    )
    raw = (tmp_path / "inbox.json").read_text(encoding="utf-8")
    assert "ingest-token-555" not in raw
    assert "PRIVATE KEY" not in raw
