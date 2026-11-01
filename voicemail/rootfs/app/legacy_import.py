"""One-time, read-only import of the pyscript inbox JSON."""
from __future__ import annotations

import hashlib
import json
import pathlib
from datetime import datetime, timezone
from typing import Any

import inbox_store

MAX_LEGACY_BYTES = 8_000_000
DEFAULT_CANDIDATES = (
    pathlib.Path("/homeassistant/pyscript/.inbound_voicemails.json"),
    pathlib.Path("/config/pyscript/.inbound_voicemails.json"),
)


def legacy_candidates(extra: str = "") -> list[pathlib.Path]:
    paths = []
    for part in (extra or "").split(","):
        part = part.strip()
        if part:
            paths.append(pathlib.Path(part))
    paths.extend(DEFAULT_CANDIDATES)
    return paths


def legacy_file_exists(paths: list[pathlib.Path]) -> bool:
    return any(path.is_file() for path in paths)


def _rows(data: Any) -> list[dict]:
    if isinstance(data, dict):
        if isinstance(data.get("voicemails"), list):
            return [row for row in data["voicemails"] if isinstance(row, dict)]
        if isinstance(data.get("items"), dict):
            return [row for row in data["items"].values() if isinstance(row, dict)]
        values = list(data.values())
        if values and all(isinstance(row, dict) for row in values):
            return values
        if not values:
            return []
        return []
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def import_legacy_once(
    store: inbox_store.VoicemailStore,
    candidates: list[pathlib.Path],
    marker: pathlib.Path,
    copy_audio_from: list[pathlib.Path] | None = None,
) -> dict:
    """Import the first legacy file once.

    The source is opened read-only. A second call returns the marker and does
    not insert again. An unreadable file does not write the marker, so a later
    start can retry. Items are upserted by id, so a retry after a crash does
    not duplicate rows.
    """
    if marker.is_file():
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                saved = {}
            return {**saved, "status": "already"}
        except (OSError, ValueError):
            return {"status": "already"}
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        return {"status": "absent"}
    try:
        size = source.stat().st_size
    except OSError as exc:
        return {"status": "error", "error": type(exc).__name__}
    if size > MAX_LEGACY_BYTES:
        return {"status": "error", "error": "too-large"}
    try:
        # Read-only. Never open this path for write.
        with open(source, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
        data = json.loads(raw_text)
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": type(exc).__name__}
    rows = _rows(data)
    imported = 0
    for row in rows:
        payload = dict(row)
        try:
            item = inbox_store.item_from_payload(payload)
        except ValueError:
            continue
        item["source"] = "legacy_pyscript"
        audio = read_mirrored_audio(item, copy_audio_from or [])
        ext = inbox_store.guess_ext(audio, "") if audio else "wav"
        store.upsert(item, audio=audio, ext=ext, preserve_heard=False)
        imported += 1
    record = {
        "status": "imported",
        "source": str(source),
        "count": imported,
        "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    inbox_store.atomic_write_bytes(marker, json.dumps(record, indent=2).encode("utf-8"))
    return record


def read_mirrored_audio(item: dict, folders: list[pathlib.Path]) -> bytes | None:
    stem = inbox_store.safe_id(item.get("call_id") or item.get("voicemail_id"))
    if not stem:
        return None
    for folder in folders:
        if not folder.is_dir():
            continue
        for ext in inbox_store.AUDIO_EXTS:
            path = folder / f"{stem}.{ext}"
            if not path.is_file():
                continue
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            except OSError:
                continue
    return None
