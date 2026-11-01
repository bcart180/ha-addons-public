"""Google Drive storage for one voicemail's audio and transcript.

Scope is drive.file only. Uploads name a parent folder and never create a
permission, so nothing is shared by link. Credentials stay in memory and are
scrubbed from log lines.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import tempfile
import time
import urllib.parse
import uuid
from typing import Any, Callable

import inbox_store

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
MAX_ATTEMPTS = 8
BACKOFF_CAP = 900


class DriveError(Exception):
    def __init__(self, code: str, transient: bool):
        super().__init__(code)
        self.code = code
        self.transient = transient


def backoff_seconds(attempt: int) -> int:
    """1, 2, 4, ... capped at 15 minutes. `attempt` is the failure count."""
    n = max(1, int(attempt))
    return min(BACKOFF_CAP, 2 ** (n - 1))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_rs256(private_key_pem: str, message: bytes) -> bytes:
    """RS256 via openssl. The PEM is a temp file mode 0600 and is removed."""
    directory = os.environ.get("VM_DATA_DIR") or None
    if directory and not os.path.isdir(directory):
        directory = None
    fd, name = tempfile.mkstemp(prefix=".cc-sign-", dir=directory)
    try:
        os.write(fd, private_key_pem.encode("utf-8"))
        os.close(fd)
        os.chmod(name, 0o600)
        proc = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", name],
            input=message,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise DriveError("sign-failed", transient=False)
        return proc.stdout
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


def build_assertion(email: str, pem: str, now: float, signer: Callable[[str, bytes], bytes]) -> str:
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = {
        "iss": email,
        "scope": DRIVE_FILE_SCOPE,
        "aud": TOKEN_URL,
        "iat": int(now),
        "exp": int(now) + 3600,
    }
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64url(signer(pem, signing_input))
    return f"{header}.{payload}.{signature}"


def multipart_related(meta: dict, media: bytes, media_type: str) -> tuple[bytes, str]:
    boundary = "callcatch_" + uuid.uuid4().hex
    meta_bytes = json.dumps(meta, separators=(",", ":")).encode("utf-8")
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
            meta_bytes,
            f"\r\n--{boundary}\r\n".encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            media,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/related; boundary={boundary}"


def parse_service_account(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    email = str(data.get("client_email") or "").strip()
    key = str(data.get("private_key") or "")
    if not email or "PRIVATE KEY" not in key:
        return None
    return {"client_email": email, "private_key": key}


class DriveClient:
    def __init__(
        self,
        *,
        folder_id: str,
        service_account: dict | None = None,
        oauth: dict | None = None,
        transport: Callable[..., Any],
        now: Callable[[], float] | None = None,
        signer: Callable[[str, bytes], bytes] | None = None,
        secrets: list[str] | None = None,
        log=None,
    ):
        self.folder_id = (folder_id or "").strip()
        self.service_account = service_account
        self.oauth = oauth or {}
        self.transport = transport
        self.now = now or time.time
        self.signer = signer or sign_rs256
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 8]
        self._token = ""
        self._exp = 0.0
        self.log = log or _NullLog()
        if service_account:
            self._note(service_account.get("private_key", ""))
        self._note(self.oauth.get("refresh_token", ""))
        self._note(self.oauth.get("client_secret", ""))

    def _note(self, value: str) -> None:
        if value and len(value) >= 8 and value not in self._secrets:
            self._secrets.append(value)

    def redact(self, text: Any) -> str:
        rendered = str(text)
        for secret in self._secrets:
            if secret and secret in rendered:
                rendered = rendered.replace(secret, "***")
        if "PRIVATE KEY" in rendered:
            rendered = "[redacted]"
        return rendered

    def _log(self, level: str, message: str, *args: Any) -> None:
        try:
            line = message % args if args else message
        except Exception:
            line = message
        getattr(self.log, level)("%s", self.redact(line))

    async def _token_request(self, form: dict) -> str:
        body = urllib.parse.urlencode(form).encode("utf-8")
        status, raw = await self._call("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body, auth=False)
        if status != 200:
            raise DriveError("auth-failed", transient=status >= 500 or status == 0)
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise DriveError("auth-failed", transient=True) from exc
        token = str(payload.get("access_token") or "")
        if not token:
            raise DriveError("auth-failed", transient=True)
        self._note(token)
        expires = int(payload.get("expires_in") or 3600)
        self._token = token
        self._exp = float(self.now()) + max(30, expires - 60)
        return token

    async def access_token(self) -> str:
        if self._token and float(self.now()) < self._exp:
            return self._token
        if self.service_account:
            assertion = build_assertion(
                self.service_account["client_email"],
                self.service_account["private_key"],
                float(self.now()),
                self.signer,
            )
            return await self._token_request(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                }
            )
        refresh = str(self.oauth.get("refresh_token") or "")
        client_id = str(self.oauth.get("client_id") or "")
        client_secret = str(self.oauth.get("client_secret") or "")
        if not (refresh and client_id and client_secret):
            raise DriveError("not-configured", transient=False)
        return await self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": DRIVE_FILE_SCOPE,
            }
        )

    async def _call(self, method: str, url: str, headers: dict, body: bytes | None, auth: bool = True) -> tuple[int, bytes]:
        sent = dict(headers)
        if auth:
            sent["Authorization"] = f"Bearer {await self.access_token()}"
        try:
            status, raw = await self.transport(method, url, sent, body)
        except Exception as exc:
            self._log("warning", "drive transport %s", type(exc).__name__ + " " + str(exc))
            raise DriveError("offline", transient=True) from exc
        if not isinstance(raw, (bytes, bytearray)):
            raw = str(raw or "").encode("utf-8")
        return int(status), bytes(raw)

    async def _authorized(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        status, raw = await self._call(method, url, headers, body, auth=True)
        if status == 401:
            self._token = ""
            self._exp = 0
            status, raw = await self._call(method, url, headers, body, auth=True)
        return status, raw

    def _file_meta(self, name: str, create: bool) -> dict:
        meta = {"name": name}
        if create:
            if not self.folder_id:
                raise DriveError("folder-missing", transient=False)
            # Parent only. No role, type, or anyone permission.
            meta["parents"] = [self.folder_id]
        return meta

    async def upload_file(self, file_id: str, name: str, media: bytes, media_type: str) -> str:
        create = not file_id
        meta = self._file_meta(name, create)
        body, content_type = multipart_related(meta, media, media_type)
        query = urllib.parse.urlencode({"uploadType": "multipart", "fields": "id", "supportsAllDrives": "true"})
        if create:
            url = f"{UPLOAD_URL}?{query}"
            method = "POST"
        else:
            url = f"{UPLOAD_URL}/{urllib.parse.quote(file_id)}?{query}"
            method = "PATCH"
        status, raw = await self._authorized(method, url, {"Content-Type": content_type}, body)
        if status in (200, 201):
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except ValueError as exc:
                raise DriveError("bad-response", transient=True) from exc
            new_id = str(payload.get("id") or file_id or "")
            if not new_id:
                raise DriveError("bad-response", transient=True)
            self._log("info", "drive upload ok name=%s", name)
            return new_id
        transient = status == 0 or status == 408 or status == 429 or status >= 500
        self._log("warning", "drive upload status=%s", status)
        raise DriveError(f"http-{status}", transient=transient)

    async def delete_file(self, file_id: str) -> None:
        if not file_id:
            return
        query = urllib.parse.urlencode({"supportsAllDrives": "true"})
        url = f"{FILES_URL}/{urllib.parse.quote(file_id)}?{query}"
        status, _raw = await self._authorized("DELETE", url, {}, None)
        if status in (200, 204, 404):
            return
        transient = status == 0 or status == 408 or status == 429 or status >= 500
        raise DriveError(f"http-{status}", transient=transient)

    async def download(self, file_id: str) -> bytes:
        query = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
        url = f"{FILES_URL}/{urllib.parse.quote(file_id)}?{query}"
        status, raw = await self._authorized("GET", url, {}, None)
        if status == 200:
            return raw
        transient = status == 0 or status == 408 or status == 429 or status >= 500
        raise DriveError(f"http-{status}", transient=transient)


class _NullLog:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None


class DriveQueue:
    """Persistent upload/delete queue. `pump_once` does not sleep."""

    def __init__(
        self,
        path: pathlib.Path,
        client: DriveClient,
        store: inbox_store.VoicemailStore,
        keep_local: bool,
        now: Callable[[], float] | None = None,
    ):
        self.path = path
        self.lock_path = path.with_suffix(".lock")
        self.client = client
        self.store = store
        self.keep_local = keep_local
        self.now = now or time.time

    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    def jobs(self) -> list[dict]:
        with inbox_store.exclusive_lock(self.lock_path):
            return self._load()

    def enqueue_upload(self, voicemail_id: str) -> None:
        if not voicemail_id:
            return
        with inbox_store.exclusive_lock(self.lock_path):
            jobs = self._load()
            for job in jobs:
                if job.get("op") == "upload" and job.get("voicemail_id") == voicemail_id:
                    job["next_try"] = 0
                    self._save_unlocked(jobs)
                    return
            jobs.append(
                {
                    "id": uuid.uuid4().hex,
                    "op": "upload",
                    "voicemail_id": voicemail_id,
                    "attempts": 0,
                    "next_try": 0,
                    "last_error": "",
                }
            )
            self._save_unlocked(jobs)

    def enqueue_delete(self, item: dict) -> None:
        audio_id = str(item.get("drive_audio_id") or "")
        transcript_id = str(item.get("drive_transcript_id") or "")
        if not audio_id and not transcript_id:
            return
        with inbox_store.exclusive_lock(self.lock_path):
            jobs = self._load()
            jobs.append(
                {
                    "id": uuid.uuid4().hex,
                    "op": "delete",
                    "voicemail_id": str(item.get("voicemail_id") or ""),
                    "drive_audio_id": audio_id,
                    "drive_transcript_id": transcript_id,
                    "attempts": 0,
                    "next_try": 0,
                    "last_error": "",
                }
            )
            self._save_unlocked(jobs)

    def _save_unlocked(self, jobs: list[dict]) -> None:
        inbox_store.atomic_write_bytes(self.path, json.dumps(jobs, indent=2).encode("utf-8"))

    def _fail(self, job: dict, code: str, transient: bool) -> None:
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["last_error"] = code[:80]
        if not transient or job["attempts"] >= MAX_ATTEMPTS:
            job["next_try"] = float(self.now()) + BACKOFF_CAP
            job["stopped"] = True
            return
        job["next_try"] = float(self.now()) + backoff_seconds(job["attempts"])

    async def pump_once(self) -> int:
        with inbox_store.exclusive_lock(self.lock_path):
            jobs = self._load()
        now = float(self.now())
        done = 0
        changed = False
        finished_ids = set()
        touched = {}
        for job in jobs:
            job_id = str(job.get("id") or "")
            if job.get("stopped"):
                continue
            if float(job.get("next_try") or 0) > now:
                continue
            try:
                if job.get("op") == "delete":
                    await self.client.delete_file(str(job.get("drive_audio_id") or ""))
                    await self.client.delete_file(str(job.get("drive_transcript_id") or ""))
                    finished_ids.add(job_id)
                    done += 1
                    changed = True
                    continue
                if job.get("op") != "upload":
                    finished_ids.add(job_id)
                    changed = True
                    continue
                if await self._upload_job(job):
                    finished_ids.add(job_id)
                    done += 1
                    changed = True
                    continue
            except DriveError as exc:
                self._fail(job, exc.code, exc.transient)
                touched[job_id] = job
                changed = True
                continue
        if changed:
            self._merge(touched, finished_ids)
        return done

    def _merge(self, touched: dict, finished_ids: set) -> None:
        with inbox_store.exclusive_lock(self.lock_path):
            fresh = []
            seen = set()
            for job in self._load():
                job_id = str(job.get("id") or "")
                seen.add(job_id)
                if job_id in finished_ids:
                    continue
                fresh.append(touched.get(job_id, job))
            for job_id, job in touched.items():
                if job_id not in seen and job_id not in finished_ids:
                    fresh.append(job)
            self._save_unlocked(fresh)

    async def _upload_job(self, job: dict) -> bool:
        vid = str(job.get("voicemail_id") or "")
        item = self.store.get(vid)
        if not item:
            return True
        stem = inbox_store.safe_id(item.get("call_id") or vid) or vid
        audio = self.store.read_audio(item)
        audio_id = str(item.get("drive_audio_id") or "")
        transcript_id = str(item.get("drive_transcript_id") or "")
        if audio:
            ext = item.get("audio_ext") or "wav"
            mime = {
                "wav": "audio/wav",
                "mp3": "audio/mpeg",
                "m4a": "audio/mp4",
                "ogg": "audio/ogg",
            }.get(ext, "application/octet-stream")
            audio_id = await self.client.upload_file(audio_id, f"{stem}.{ext}", audio, mime)
            self.store.update_fields(vid, drive_audio_id=audio_id, drive_status="pending")
        public = {k: v for k, v in item.items() if k not in ("drive_status",)}
        public["drive_audio_id"] = audio_id
        blob = json.dumps(public, indent=2, sort_keys=True).encode("utf-8")
        transcript_id = await self.client.upload_file(transcript_id, f"{stem}.json", blob, "application/json")
        self.store.update_fields(
            vid,
            drive_audio_id=audio_id,
            drive_transcript_id=transcript_id,
            drive_status="uploaded",
            has_audio=bool(audio_id) or bool(item.get("has_audio")),
        )
        if not self.keep_local and audio:
            self.store.delete_local_audio(vid)
            self.store.update_fields(vid, has_audio=bool(audio_id))
        return True
