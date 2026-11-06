"""Read-only ingress server. Serves the panel and re-reads a snapshot file. No usage API."""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import snapshot

log = logging.getLogger("usage_dashboard")

BUNDLED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundled", "usage_snapshot.json")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".woff2": "font/woff2",
}
STATIC_SUFFIXES = set(CONTENT_TYPES)


class Config:
    def __init__(self, host, port, allow, options_path, bundled_path, static_dir, data_root):
        self.host = host
        self.port = port
        self.allow = allow
        self.options_path = options_path
        self.bundled_path = bundled_path
        self.static_dir = static_dir
        self.data_root = data_root
        self.static = load_static(static_dir)


def load_static(static_dir: str) -> dict:
    files = {}
    root = os.path.abspath(static_dir)
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            suffix = os.path.splitext(name)[1]
            if suffix not in STATIC_SUFFIXES:
                continue
            rel = "/" + os.path.relpath(path, root).replace(os.sep, "/")
            with open(path, "rb") as handle:
                files[rel] = (CONTENT_TYPES[suffix], handle.read())
    if "/" not in files and "/index.html" in files:
        files["/"] = files["/index.html"]
    elif "/index.html" in files:
        files["/"] = files["/index.html"]
    return files


def load_options(path: str) -> dict:
    defaults = {"snapshot_path": "/data/usage_snapshot.json", "log_level": "info"}
    if not path or not os.path.isfile(path):
        return defaults
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        log.warning("options file could not be read; using defaults")
        return defaults
    if not isinstance(raw, dict):
        return defaults
    if isinstance(raw.get("snapshot_path"), str) and raw["snapshot_path"].strip():
        defaults["snapshot_path"] = raw["snapshot_path"].strip()
    if raw.get("log_level") in ("debug", "info", "warning", "error"):
        defaults["log_level"] = raw["log_level"]
    return defaults


def read_snapshot(config: Config) -> tuple[dict, str | None]:
    """Return (body, warning). body is the API object. Never fetches usage."""
    options = load_options(config.options_path)
    path = options["snapshot_path"]
    warning = None
    if path:
        if not snapshot.inside_directory(path, config.data_root) or not path.endswith(".json"):
            warning = (
                "The configured snapshot path must be a .json file inside the add-on "
                "data directory. Showing the bundled snapshot."
            )
        elif os.path.isfile(path):
            try:
                data = snapshot.validate(snapshot.load_json(path))
            except json.JSONDecodeError:
                warning = "The data snapshot is not valid JSON. Showing the bundled snapshot."
            except snapshot.SnapshotError as exc:
                warning = (
                    "The data snapshot does not match schema version 1 "
                    f"({exc}). Showing the bundled snapshot."
                )
            except OSError:
                warning = "The data snapshot could not be read. Showing the bundled snapshot."
            else:
                log.info("served data snapshot %s", data.get("snapshot_label"))
                return _body(data, "data", None), None
    try:
        data = snapshot.validate(snapshot.load_json(config.bundled_path))
    except (OSError, json.JSONDecodeError, snapshot.SnapshotError) as exc:
        log.error("bundled snapshot unreadable: %s", exc)
        return {
            "ok": False,
            "error": "The bundled snapshot could not be read. No usage figures are shown.",
        }, None
    if warning is None and path and not os.path.isfile(path):
        log.info("no data snapshot; serving bundled %s", data.get("snapshot_label"))
    elif warning:
        log.info("data snapshot rejected; serving bundled")
    return _body(data, "bundled", warning), warning


def _body(data: dict, provenance: str, warning: str | None) -> dict:
    view = snapshot.build_view(data, provenance)
    return {"ok": True, "warning": warning, "view": view}


def make_handler(config: Config):
    class Handler(BaseHTTPRequestHandler):
        server_version = "UsageDashboard"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _allowed(self) -> bool:
            if self.client_address[0] in config.allow:
                return True
            body = b"ingress only"
            self._send(403, "text/plain; charset=utf-8", body)
            return False

        def _send(self, status: int, content_type: str, body: bytes, extra=None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "font-src 'self'; img-src 'self'; script-src 'self'; "
                "frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
            )
            self.send_header("Connection", "close")
            if extra:
                for key, value in extra:
                    self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps(obj).encode())

        def do_GET(self):
            if not self._allowed():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                self._json(200, {"ok": True})
                return
            if path == "/api/snapshot":
                body, _warning = read_snapshot(config)
                self._json(200 if body.get("ok") else 500, body)
                return
            static = config.static.get(path)
            if static is None:
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            content_type, payload = static
            self._send(200, content_type, payload)

        def do_HEAD(self):
            if self._allowed():
                self._send(405, "text/plain; charset=utf-8", b"read-only")

        def _deny(self):
            if self._allowed():
                self._json(405, {"ok": False, "error": "read-only"})

        do_POST = do_PUT = do_PATCH = do_DELETE = _deny

    return Handler


def make_httpd(config: Config) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((config.host, config.port), make_handler(config))


def config_from_env() -> Config:
    allow = {item.strip() for item in os.environ.get("UD_ALLOW", "172.30.32.2").split(",") if item.strip()}
    return Config(
        host=os.environ.get("UD_HOST", "0.0.0.0"),
        port=int(os.environ.get("UD_PORT", "8098")),
        allow=allow,
        options_path=os.environ.get("UD_OPTIONS", "/data/options.json"),
        bundled_path=os.environ.get("UD_BUNDLED", BUNDLED_PATH),
        static_dir=os.environ.get("UD_STATIC", STATIC_DIR),
        data_root=os.environ.get("UD_DATA_ROOT", "/data"),
    )


def main() -> None:
    config = config_from_env()
    level_name = load_options(config.options_path).get("log_level", "info").upper()
    logging.basicConfig(level=getattr(logging, level_name, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")
    httpd = make_httpd(config)
    log.info("usage dashboard listening on %s:%s", config.host, config.port)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
