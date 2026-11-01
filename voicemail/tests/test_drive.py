"""Drive uploads with a mocked HTTP client. No live Google calls."""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys
import urllib.parse

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(ROOT))

import drive_backend
import inbox_store

FOLDER = "folder-555"
REFRESH = "refresh-555-secret"
PEM = "private-key-material-555-not-real-value"


class ListLog:
    def __init__(self):
        self.lines = []

    def info(self, msg, *args):
        self.lines.append(msg % args if args else msg)

    def warning(self, msg, *args):
        self.lines.append(msg % args if args else msg)


class Clock:
    def __init__(self, start=1_000.0):
        self.t = start

    def __call__(self):
        return self.t


def _meta(body: bytes) -> dict:
    decoder = json.JSONDecoder()
    idx = body.find(b"{")
    meta, _end = decoder.raw_decode(body[idx:].decode("utf-8"))
    return meta


def _store(tmp_path):
    return inbox_store.VoicemailStore(tmp_path / "inbox.json", tmp_path / "audio")


def _item():
    return inbox_store.item_from_payload(
        {
            "call_id": "CA5550100",
            "from": "+15555550100",
            "date": "2026-10-29T14:00:00+00:00",
            "transcript_text": "Please call 555-555-0101.",
            "duration_sec": 4,
            "source": "test",
        }
    )


@pytest.mark.asyncio
async def test_service_account_scope_and_no_public_share():
    calls = []

    async def transport(method, url, headers, body):
        calls.append((method, url, headers, body))
        if "oauth2.googleapis.com/token" in url:
            return 200, b'{"access_token":"ya29-test-token","expires_in":3600}'
        return 200, json.dumps({"id": f"fid-{len(calls)}"}).encode()

    def signer(_pem, _message):
        return b"\x00\x01sig"

    log = ListLog()
    client = drive_backend.DriveClient(
        folder_id=FOLDER,
        service_account={"client_email": "callcatch@example.invalid", "private_key": PEM},
        transport=transport,
        now=Clock(),
        signer=signer,
        secrets=[PEM, REFRESH],
        log=log,
    )
    file_id = await client.upload_file("", "CA5550100.wav", b"RIFF\x00\x00\x00\x00WAVEsample", "audio/wav")
    assert file_id.startswith("fid-")
    token_call = calls[0]
    form = urllib.parse.parse_qs(token_call[3].decode())
    assertion = form["assertion"][0]
    payload = assertion.split(".")[1]
    pad = "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload + pad))
    assert claims["scope"] == drive_backend.DRIVE_FILE_SCOPE
    assert PEM not in token_call[3].decode()
    assert form["grant_type"][0] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    upload = calls[1]
    assert upload[0] == "POST"
    assert "/upload/drive/v3/files" in upload[1]
    assert "fields=id" in upload[1]
    assert "permissions" not in upload[1]
    assert "webViewLink" not in upload[1]
    meta = _meta(upload[3])
    assert meta["parents"] == [FOLDER]
    assert "role" not in meta
    assert "type" not in meta
    assert "anyone" not in meta
    assert b"anyone" not in upload[3]
    assert upload[2]["Authorization"] == "Bearer ya29-test-token"
    await client.upload_file(file_id, "CA5550100.wav", b"RIFF\x00\x00\x00\x00WAVEsample", "audio/wav")
    token_posts = [url for _m, url, _h, _b in calls if "oauth2.googleapis.com/token" in url]
    assert len(token_posts) == 1
    assert all("refresh-555-secret" not in line for line in log.lines)
    assert PEM not in "\n".join(log.lines)


@pytest.mark.asyncio
async def test_oauth_refresh_uses_drive_file_scope_and_redacts_logs():
    calls = []

    async def transport(method, url, headers, body):
        calls.append((method, url, body))
        if "oauth2.googleapis.com/token" in url:
            raise OSError(f"offline {REFRESH}")
        return 200, b"{}"

    log = ListLog()
    client = drive_backend.DriveClient(
        folder_id=FOLDER,
        oauth={
            "refresh_token": REFRESH,
            "client_id": "client-555",
            "client_secret": "secret-555-value",
        },
        transport=transport,
        now=Clock(),
        log=log,
    )
    with pytest.raises(drive_backend.DriveError):
        await client.access_token()
    form = urllib.parse.parse_qs(calls[0][2].decode())
    assert form["grant_type"][0] == "refresh_token"
    assert form["scope"][0] == drive_backend.DRIVE_FILE_SCOPE
    assert form["refresh_token"][0] == REFRESH
    blob = "\n".join(log.lines)
    assert REFRESH not in blob
    assert "secret-555-value" not in blob
    assert "***" in blob


@pytest.mark.asyncio
async def test_queue_retries_then_uploads_pair(tmp_path):
    store = _store(tmp_path)
    store.upsert(_item(), audio=b"RIFF\x00\x00\x00\x00WAVEsample", ext="wav")
    fail = {"n": 1}
    calls = []

    async def transport(method, url, headers, body):
        calls.append(url)
        if "oauth2.googleapis.com/token" in url:
            return 200, b'{"access_token":"ya29-test-token","expires_in":3600}'
        if fail["n"] > 0:
            fail["n"] -= 1
            raise OSError("offline")
        return 200, json.dumps({"id": f"fid-{len(calls)}"}).encode()

    clock = Clock()
    client = drive_backend.DriveClient(
        folder_id=FOLDER,
        oauth={"refresh_token": REFRESH, "client_id": "client-555", "client_secret": "secret-555-value"},
        transport=transport,
        now=clock,
    )
    queue = drive_backend.DriveQueue(tmp_path / "queue.json", client, store, keep_local=True, now=clock)
    queue.enqueue_upload("CA5550100")
    assert await queue.pump_once() == 0
    job = queue.jobs()[0]
    assert job["attempts"] == 1
    assert job["next_try"] == clock.t + drive_backend.backoff_seconds(1)
    assert store.audio_file(store.get("CA5550100")) is not None
    clock.t = job["next_try"]
    assert await queue.pump_once() == 1
    assert queue.jobs() == []
    saved = store.get("CA5550100")
    assert saved["drive_status"] == "uploaded"
    assert saved["drive_audio_id"]
    assert saved["drive_transcript_id"]
    assert saved["drive_audio_id"] != saved["drive_transcript_id"]
    uploads = [url for url in calls if "/upload/drive/v3/files" in url]
    assert len(uploads) == 3  # one failed attempt, then audio and transcript
    assert all("permissions" not in url for url in calls)
    raw_queue = (tmp_path / "queue.json").read_text(encoding="utf-8") if (tmp_path / "queue.json").exists() else ""
    assert REFRESH not in raw_queue
    assert store.read_audio(saved) is not None


@pytest.mark.asyncio
async def test_keep_local_false_deletes_after_upload_and_delete_is_queued(tmp_path):
    store = _store(tmp_path)
    saved = store.upsert(_item(), audio=b"RIFF\x00\x00\x00\x00WAVEsample", ext="wav")
    path = store.audio_file(saved)
    assert path is not None

    async def transport(method, url, headers, body):
        if "oauth2.googleapis.com/token" in url:
            return 200, b'{"access_token":"ya29-test-token","expires_in":3600}'
        if method == "DELETE":
            return 204, b""
        if "alt=media" in url:
            return 200, b"RIFF\x00\x00\x00\x00WAVEsample"
        return 200, b'{"id":"fid-555"}'

    clock = Clock()
    client = drive_backend.DriveClient(
        folder_id=FOLDER,
        service_account={"client_email": "callcatch@example.invalid", "private_key": PEM},
        transport=transport,
        now=clock,
        signer=lambda _pem, _msg: b"sig",
    )
    queue = drive_backend.DriveQueue(tmp_path / "queue.json", client, store, keep_local=False, now=clock)
    queue.enqueue_upload("CA5550100")
    assert await queue.pump_once() == 1
    assert not path.exists()
    current = store.get("CA5550100")
    assert current["audio_local"] is False
    assert current["has_audio"] is True
    assert current["drive_audio_id"] == "fid-555"
    data = await client.download(current["drive_audio_id"])
    assert data.startswith(b"RIFF")
    removed = store.clear_one("CA5550100")
    queue.enqueue_delete(removed)
    assert await queue.pump_once() == 1
    assert queue.jobs() == []


def test_backoff_grows_and_caps():
    assert drive_backend.backoff_seconds(1) == 1
    assert drive_backend.backoff_seconds(2) == 2
    assert drive_backend.backoff_seconds(3) == 4
    assert drive_backend.backoff_seconds(20) == drive_backend.BACKOFF_CAP


def test_openssl_signs_drive_file_scope(tmp_path):
    key = tmp_path / "key.pem"
    pub = tmp_path / "pub.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)],
        check=True,
        capture_output=True,
    )
    subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True, capture_output=True)
    pem = key.read_text(encoding="utf-8")
    assertion = drive_backend.build_assertion("callcatch@example.invalid", pem, 1_700_000_000, drive_backend.sign_rs256)
    header, payload, sig = assertion.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["scope"] == "https://www.googleapis.com/auth/drive.file"
    assert "PRIVATE KEY" not in assertion
    signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    sig_path = tmp_path / "sig.bin"
    data_path = tmp_path / "input.bin"
    data_path.write_bytes(f"{header}.{payload}".encode("ascii"))
    sig_path.write_bytes(signature)
    verified = subprocess.run(
        ["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig_path), str(data_path)],
        check=False,
        capture_output=True,
    )
    assert verified.returncode == 0
    assert b"Verified OK" in verified.stdout
