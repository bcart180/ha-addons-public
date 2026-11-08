"""Sports News ingress server. Read-only page. It does not call X."""

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
DEFAULT_INBOXES = (
    "/homeassistant/custom_templates/sports_news/inbox.json",
    "/config/custom_templates/sports_news/inbox.json",
    "/share/sports_news/inbox.json",
)
ALLOWED_ROOTS = (
    "/homeassistant/custom_templates/sports_news",
    "/config/custom_templates/sports_news",
    "/share/sports_news",
)
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}


def inbox_list(options):
    custom = ""
    if isinstance(options, dict) and isinstance(options.get("inbox_file"), str):
        custom = options["inbox_file"].strip()
    if not custom:
        return list(DEFAULT_INBOXES)
    if custom not in DEFAULT_INBOXES:
        raise validate.ValidationError("inbox_file is not an allowed publish path")
    return [custom]


def _inside(path, roots):
    real = os.path.realpath(path)
    for root in roots:
        base = os.path.realpath(root)
        if real == base or real.startswith(base + os.sep):
            return real
    return None


class Service:
    def __init__(self, preview_path, data_dir, inbox_paths, allowed_roots, config_error=None):
        self.preview_path = preview_path
        self.data_dir = data_dir
        self.inbox_paths = list(inbox_paths)
        self.allowed_roots = tuple(allowed_roots)
        self.config_error = config_error
        self.lock = threading.Lock()

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
            doc = self._read(self.preview_path, now)
            return validate.view(doc, now, origin="bundled_starter")
        try:
            raw = self._read_raw(found)
            doc = validate.load_bytes(raw, now)
        except validate.ValidationError as exc:
            return self._fallback(now, str(exc))
        if doc["status"] == "error" and not doc["items"]:
            last = self._read_last(now)
            if last is not None:
                return validate.view(
                    last, now, origin="last_good", source_error=doc.get("source_error"),
                    attempt_at=doc.get("last_attempt_at"), freshness="error",
                )
            return validate.view(doc, now, origin="error", freshness="error")
        self._write_last(raw)
        return validate.view(doc, now, origin="published")

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
            real = _inside(path, self.allowed_roots)
            if real is None:
                return None, "inbox path is outside the allowed publish directories"
            if os.path.isfile(real):
                return real, None
        return None, None

    def _read_raw(self, path):
        with open(path, "rb") as handle:
            return handle.read(validate.MAX_BYTES + 1)

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
        tmp = os.path.join(self.data_dir, ".last_good.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._last_path())


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
        paths = inbox_list(options)
        config_error = None
    except validate.ValidationError as exc:
        paths = []
        config_error = str(exc)
    service = Service(
        PREVIEW_PATH,
        os.environ.get("SN_DATA", "/data"),
        paths,
        ALLOWED_ROOTS,
        config_error,
    )
    allow = {item.strip() for item in os.environ.get("SN_ALLOW", "172.30.32.2").split(",") if item.strip()}
    port = int(os.environ.get("SN_PORT", "8099"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(service, allow, STATIC_DIR))
    log.info("Sports News listening on %s", port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
