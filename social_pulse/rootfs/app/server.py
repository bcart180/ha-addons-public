"""Social Pulse ingress server (stdlib only). Serves the panel and /api/snapshot."""
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import collectors

OPTIONS = os.environ.get("SP_OPTIONS", "/data/options.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = int(os.environ.get("SP_PORT", "8099"))
# Ingress-only: the Supervisor ingress proxy (and watchdog) is 172.30.32.2. Everyone else gets 403.
ALLOW = {a.strip() for a in os.environ.get("SP_ALLOW", "172.30.32.2").split(",") if a.strip()}
log = logging.getLogger("social_pulse")
_state = {"snap": {"generated_at": None, "x": [], "sites": [], "errors": ["first refresh pending"]}}
# Production documents keep this policy. The trial document adds the base-script hash.
_PROD_CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'self'"


def _trial_document():
    """Return (html bytes, CSP) for the UI-4 trial. Hash matches the inline base script."""
    path = os.path.join(STATIC, "ui-trial", "index.html")
    with open(path, "rb") as handle:
        body = handle.read()
    match = re.search(br"<script>(.*?)</script>", body, re.S)
    if not match:
        raise RuntimeError("trial base script missing")
    digest = base64.b64encode(hashlib.sha256(match.group(1)).digest()).decode("ascii")
    csp = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "script-src 'self' 'sha256-%s'; frame-ancestors 'self'; base-uri 'self'" % digest
    )
    return body, csp


def load_options():
    with open(OPTIONS) as f:
        return json.load(f)


def refresh(opts):
    snap_file = opts.get("snapshot_file")
    if snap_file:  # offline preview / tests: a normalized snapshot on disk
        with open(snap_file) as f:
            _state["snap"] = json.load(f)
        return
    _state["snap"] = collectors.collect(opts)
    log.info("refreshed: %d accounts, %d sites, %d errors", len(_state["snap"]["x"]),
             len(_state["snap"]["sites"]), len(_state["snap"]["errors"]))


def loop(opts):
    while True:
        try:
            refresh(opts)
        except Exception as e:  # noqa: BLE001
            log.error("refresh failed: %s", collectors.redact(e, [opts.get("x_bearer_token"), opts.get("cloudflare_api_token")]))
        time.sleep(60 * int(opts.get("refresh_minutes", 30)))


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def log_message(self, fmt, *args):  # no request logging (keeps ingress tokens out of logs)
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", getattr(self, "_csp", _PROD_CSP))
        super().end_headers()

    def _allowed(self):
        if self.client_address[0] in ALLOW:
            return True
        self.send_error(403, "ingress only")
        return False

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect_trial(self):
        # Relative Location stays inside the ingress prefix. /ui-trial itself is 200, so this does not loop.
        self.send_response(302)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.send_header("Location", "../ui-trial")
        self.end_headers()

    def _serve_trial(self, send_body):
        body, csp = _trial_document()
        self._csp = csp
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def do_GET(self):
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            return self._json({"ok": True})
        if path == "/api/snapshot":
            return self._json(_state["snap"])
        if path in ("/ui-trial/", "/ui-trial/index.html"):
            return self._redirect_trial()
        if path == "/ui-trial":
            return self._serve_trial(True)
        return super().do_GET()

    def do_HEAD(self):
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/ui-trial/", "/ui-trial/index.html"):
            return self._redirect_trial()
        if path == "/ui-trial":
            return self._serve_trial(False)
        super().do_HEAD()

    def _deny(self):
        if self._allowed():
            self.send_error(405, "read-only")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _deny


def main():
    opts = load_options()
    logging.basicConfig(level=getattr(logging, opts.get("log_level", "info").upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=loop, args=(opts,), daemon=True).start()
    log.info("Social Pulse listening on %d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
