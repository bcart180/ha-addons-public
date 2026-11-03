"""Schema, translations, and the blank speaker default."""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = (ROOT / "config.yaml").read_text(encoding="utf-8")
TRANSLATIONS = (ROOT / "translations" / "en.yaml").read_text(encoding="utf-8")
DOCS = (ROOT / "DOCS.md").read_text(encoding="utf-8")

NEW_OPTIONS = (
    "ingest_token",
    "legacy_pyscript",
    "storage_backend",
    "keep_local_copy",
    "google_drive_folder_id",
    "google_service_account_json",
    "google_oauth_client_id",
    "google_oauth_client_secret",
    "google_oauth_refresh_token",
    "voice_trust_url",
    "voice_trust_token",
    "contacts_sync_enabled",
    "icloud_apple_id",
    "icloud_app_password",
    "contacts_sync_interval_min",
)


def test_slug_version_and_blank_player():
    assert "\nslug: voicemail\n" in CONFIG
    assert 'version: "0.5.5"' in CONFIG
    assert 'media_player: ""' in CONFIG
    assert "bedroom_homepod" not in CONFIG
    assert "HomePod" not in CONFIG
    assert "homeassistant_config:ro" in CONFIG
    assert "homeassistant_config:rw" not in CONFIG
    assert "\npanel_admin: true\n" in CONFIG
    assert "share:rw" in CONFIG
    assert "media:rw" in CONFIG
    for key in ("startup:", "boot:", "ingress_port:", "watchdog:", "hassio_role:", "backup:"):
        assert f"\n{key}" not in f"\n{CONFIG}"
    assert "http://local-voicemail:8099/api/ingest" in DOCS
    assert "http://<repohash>-voicemail:8099/api/ingest" in DOCS
    assert "http://voicemail:8099" not in DOCS


def test_password_options_and_translations():
    for name in (
        "ingest_token",
        "google_service_account_json",
        "google_oauth_client_secret",
        "google_oauth_refresh_token",
    ):
        assert f"{name}: password" in CONFIG
    assert "icloud_app_password: password" in CONFIG
    assert "contacts_sync_enabled: false" in CONFIG
    assert "contacts_sync_interval_min: 30" in CONFIG
    assert "contacts_sync_interval_min: int(15," in CONFIG
    assert "storage_backend: list(local|google_drive)" in CONFIG
    assert "legacy_pyscript: list(auto|on|off)" in CONFIG
    for name in NEW_OPTIONS:
        assert f"  {name}:" in TRANSLATIONS
    assert "drive.file" in TRANSLATIONS
    assert "drive.file" in DOCS
    assert "anyone with the link" in DOCS
    assert "/api/ingest" in DOCS
