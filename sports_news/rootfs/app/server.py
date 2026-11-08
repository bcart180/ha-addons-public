"""Sports News ingress server. Read-only page. It does not call X."""

import errno
import json
import logging
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import validate

log = logging.getLogger("sports_news")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PREVIEW_PATH = os.path.join(APP_DIR, "preview", "starter_2026-11-08T095038Z.json")
STATIC_DIR = os.path.join(APP_DIR, "static")
# Production does not mount Home Assistant config or /share. Those trees hold
# private files, and read-only access would still disclose them. There is no
# sanctioned writer that can place a snapshot in a narrower path this process
# can see, so the installed add-on is snapshot-only. Tests pass an explicit
# inbox directory when they exercise the publish rules.
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def inbox_list(options):
    """Publishing from a host path is not enabled.

    ha_write_file can write custom_templates inside the Home Assistant config,
    and reading that file back requires the whole config mount. /share is a
    separate broad mount and is not enabled for this writer. Neither is used.
    """
    custom = ""
    if isinstance(options, dict) and isinstance(options.get("inbox_file"), str):
        custom = options["inbox_file"].strip()
    if custom:
        raise validate.ValidationError(
            "inbox publishing is not enabled; mounting Home Assistant config or share would expose private files"
        )
    return []


def _lexical_inside(path, root):
    path = os.path.normpath(os.path.abspath(path))
    root = os.path.normpath(os.path.abspath(root))
    return path == root or path.startswith(root + os.sep)


def read_nofollow(path, roots):
    """Read a file without following a symlink at any path component.

    realpath is not used. If the allowed directory itself is a symlink, opening
    it fails instead of treating the link target as the new allowed root.
    """
    path = os.path.normpath(os.path.abspath(path))
    if not any(_lexical_inside(path, root) for root in roots):
        raise validate.ValidationError("inbox path is outside the allowed publish directories")
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = [part for part in path.split(os.sep) if part]
        for index, part in enumerate(parts):
            is_last = index == len(parts) - 1
            try:
                nxt = os.open(part, flags_file if is_last else flags_dir, dir_fd=fd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None
                if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EPERM):
                    raise validate.ValidationError(
                        "inbox path is outside the allowed publish directories"
                    ) from None
                raise
            os.close(fd)
            fd = nxt
        chunks = []
        total = 0
        while total <= validate.MAX_BYTES:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
            total += len(block)
        return b"".join(chunks)
    finally:
        os.close(fd)


class Service:
    def __init__(self, preview_path, data_dir, inbox_paths, allowed_roots, config_error=None):
        self.preview_path = preview_path
        self.data_dir = data_dir
        self.inbox_paths = list(inbox_paths)
        self.allowed_roots = tuple(allowed_roots)
        self.config_error = config_error
        self.startup_note = None
        self.startup_attempt = None
        self.lock = threading.Lock()

    def apply_startup_option(self, options, now):
        """Validate snapshot_json once, at process start.

        Supervisor writes /data/options.json before the app starts. It does not
        update that file again until the next start. A bad value must not exit
        the process: the watchdog checks /api/health, which stays healthy.
        """
        self.startup_note = None
        self.startup_attempt = None
        raw_text = ""
        if isinstance(options, dict):
            raw_text = options.get("snapshot_json", "")
        if raw_text in (None, ""):
            return
        if not isinstance(raw_text, str):
            self.startup_note = (
                "Snapshot option rejected (snapshot_json must be text). "
                "Showing the last valid snapshot when one was saved. "
                "Its collection time was not reset."
            )
            return
        try:
            doc = validate.load_bytes(raw_text.encode("utf-8"), now)
        except validate.ValidationError as exc:
            self._reject_option(str(exc))
            return
        last = self._read_last(now)
        if doc["status"] == "error":
            self.startup_attempt = doc.get("last_attempt_at")
            self._reject_option(doc.get("source_error") or "snapshot status is error")
            return
        try:
            merged = validate.apply_publication(last, doc)
        except validate.RollbackError as exc:
            self._reject_option(str(exc))
            return
        self._write_last(validate.canonical_bytes(merged))

    def _reject_option(self, reason):
        if os.path.isfile(self._last_path()):
            self.startup_note = (
                "Snapshot option rejected (" + reason + "). "
                "Showing the last valid snapshot. Its collection time was not reset. "
                "The app keeps running, so this bad option does not cause another restart."
            )
            return
        self.startup_note = (
            "Snapshot option rejected (" + reason + "). "
            "No saved snapshot yet, so the starter snapshot is shown. It is not the rejected option. "
            "The app keeps running, so this bad option does not cause another restart."
        )

    def page(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self.lock:
            try:
                return self._page(now)
            except validate.ValidationError as exc:
                log.warning("snapshot rejected")
                return self._fallback(now, str(exc))
            except Exception:
                log.warning("snapshot read failed")
                return self._fallback(now, "The snapshot could not be read.")

    def _page(self, now):
        if self.config_error:
            return self._fallback(now, self.config_error)
        found, problem = self._inbox()
        if problem:
            return self._fallback(now, problem)
        if found is None:
            last = self._read_last(now)
            if self.inbox_paths and last is not None:
                return validate.view(
                    last, now, origin="last_good",
                    inbox_error="Inbox file is missing.",
                    freshness="error",
                    startup_note=self.startup_note,
                )
            if last is not None:
                freshness = "error" if self.startup_note else None
                return validate.view(
                    last, now, origin="last_good", freshness=freshness,
                    startup_note=self.startup_note,
                    attempt_at=self.startup_attempt,
                )
            doc = self._read(self.preview_path, now)
            return validate.view(
                doc, now, origin="bundled_starter",
                startup_note=self.startup_note,
                freshness="error" if self.startup_note else None,
            )
        try:
            raw = self._read_raw(found)
            doc = validate.load_bytes(raw, now)
        except validate.ValidationError as exc:
            return self._fallback(now, str(exc))
        last = self._read_last(now)
        if doc["status"] == "error":
            if last is not None:
                return validate.view(
                    last, now, origin="last_good", source_error=doc.get("source_error"),
                    attempt_at=doc.get("last_attempt_at"), freshness="error",
                )
            return validate.view(doc, now, origin="error", freshness="error")
        try:
            merged = validate.apply_publication(last, doc)
        except validate.RollbackError as exc:
            if last is None:
                return self._fallback(now, str(exc))
            return validate.view(last, now, origin="last_good", inbox_error=str(exc), freshness="error")
        self._write_last(validate.canonical_bytes(merged))
        return validate.view(merged, now, origin="published")

    def _fallback(self, now, reason):
        last = self._read_last(now)
        if last is not None:
            return validate.view(last, now, origin="last_good", inbox_error=reason, freshness="error")
        try:
            doc = self._read(self.preview_path, now)
        except (OSError, validate.ValidationError):
            return validate.empty_view(now, reason)
        return validate.view(doc, now, origin="bundled_starter", inbox_error=reason, freshness="error")

    def _inbox(self):
        for path in self.inbox_paths:
            try:
                raw = read_nofollow(path, self.allowed_roots)
            except validate.ValidationError as exc:
                return None, str(exc)
            if raw is not None:
                return path, None
        return None, None

    def _read_raw(self, path):
        if path == self.preview_path or path == self._last_path():
            with open(path, "rb") as handle:
                return handle.read(validate.MAX_BYTES + 1)
        raw = read_nofollow(path, self.allowed_roots)
        if raw is None:
            raise validate.ValidationError("Inbox file is missing.")
        return raw

    def _read(self, path, now):
        return validate.load_bytes(self._read_raw(path), now)

    def _last_path(self):
        return os.path.join(self.data_dir, "last_good.json")

    def _read_last(self, now):
        path = self._last_path()
        if not os.path.isfile(path):
            return None
        try:
            return self._read(path, now)
        except (OSError, validate.ValidationError):
            log.warning("last good snapshot could not be read")
            return None

    def _write_last(self, payload):
        os.makedirs(self.data_dir, exist_ok=True)
        dest = self._last_path()
        if os.path.isfile(dest):
            try:
                with open(dest, "rb") as existing:
                    current = existing.read()
            except OSError:
                current = None
            if current == payload:
                return
        tmp = os.path.join(self.data_dir, ".last_good.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, dest)
        dir_fd = os.open(self.data_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def make_handler(service, allow, static_dir):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sports-news"
        sys_version = ""

        def log_message(self, fmt, *args):
            return

        def send_error(self, code, message=None, explain=None):
            body = f"{code}\n".encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _allowed(self):
            if self.client_address[0] in allow:
                return True
            self.send_error(403, "ingress only")
            return False

        def _security(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'none'; font-src 'none'; connect-src 'self'; frame-src 'none'; "
                             "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

        def _send(self, code, body, content_type):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._security()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, obj):
            self._send(200, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _static(self, path):
            name = STATIC_FILES.get(path)
            if not name:
                self.send_error(404)
                return
            full = os.path.realpath(os.path.join(static_dir, name))
            if not full.startswith(os.path.realpath(static_dir) + os.sep):
                self.send_error(404)
                return
            with open(full, "rb") as handle:
                self._send(200, handle.read(), CONTENT_TYPES[name])

        def do_GET(self):
            self._route()

        def do_HEAD(self):
            self._route()

        def _route(self):
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                return self._json({"ok": True})
            if path == "/api/snapshot":
                return self._json(service.page())
            if path in STATIC_FILES:
                return self._static(path)
            self.send_error(404)

        def _deny(self):
            if self._allowed():
                self.send_error(405, "read-only")

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _deny

    return Handler


def load_options(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def main():
    options_path = os.environ.get("SN_OPTIONS", "/data/options.json")
    options = load_options(options_path)
    level = options.get("log_level", "info")
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        inbox_list(options)
        config_error = None
    except validate.ValidationError as exc:
        config_error = str(exc)
    service = Service(
        PREVIEW_PATH,
        os.environ.get("SN_DATA", "/data"),
        [],
        (),
        config_error,
    )
    try:
        service.apply_startup_option(options, datetime.now(timezone.utc))
    except Exception:
        log.warning("startup option was not applied")
        service.startup_note = (
            "Snapshot option could not be read. Showing the last valid snapshot when one was saved. "
            "The app keeps running."
        )
    allow = {item.strip() for item in os.environ.get("SN_ALLOW", "172.30.32.2").split(",") if item.strip()}
    port = int(os.environ.get("SN_PORT", "8099"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service, allow, STATIC_DIR))
    log.info("Sports News listening on %s", port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
