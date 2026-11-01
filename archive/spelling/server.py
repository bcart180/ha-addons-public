#!/usr/bin/env python3
"""Spelling add-on: tiny read-only static server for HA ingress.

Serves files from ROOT (default /share/spelling, mapped read-only) at the root
of the ingress path. "/" returns the index file directly (no redirect), so the
page's relative URLs such as fetch('spelling-lists.json') resolve under
/api/hassio_ingress/<token>/. GET and HEAD only; no directory listings; no
files are copied into the image.
"""
import json
import mimetypes
import os
import posixpath
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OPTIONS = "/data/options.json"
opts = {}
try:
    with open(OPTIONS) as fh:
        opts = json.load(fh)
except (OSError, ValueError):
    pass
ROOT = os.path.realpath(os.environ.get("SPELLING_ROOT") or opts.get("root") or "/share/spelling")
INDEX = os.environ.get("SPELLING_INDEX") or opts.get("index") or "spelling.html"
PORT = int(os.environ.get("SPELLING_PORT", "8099"))
# Only the Supervisor ingress proxy may connect (HA best practice). Override for local tests.
ALLOW = [a for a in os.environ.get("SPELLING_ALLOW", "172.30.32.2").split(",") if a]

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".woff2": "font/woff2",
}


def ctype(path):
    ext = os.path.splitext(path)[1].lower()
    return TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    server_version = "Spelling/0.1"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stdout.write("%s %s\n" % (self.address_string(), fmt % args))

    def _deny_client(self):
        if ALLOW and self.client_address[0] not in ALLOW:
            self.send_error(HTTPStatus.FORBIDDEN)
            return True
        return False

    def _resolve(self):
        path = urllib.parse.urlsplit(self.path).path
        path = urllib.parse.unquote(path)
        if path in ("", "/"):
            path = "/" + INDEX
        norm = posixpath.normpath(path).lstrip("/")
        if norm.startswith("..") or "\x00" in norm:
            return None
        full = os.path.realpath(os.path.join(ROOT, norm))
        if full != ROOT and not full.startswith(ROOT + os.sep):
            return None  # traversal or symlink out of ROOT
        if os.path.isdir(full):
            full = os.path.join(full, INDEX)
        return full if os.path.isfile(full) else None

    def _serve(self, body):
        if self.path.split("?", 1)[0] == "/healthz":
            data = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)
            return
        if self._deny_client():
            return
        full = self._resolve()
        if not full:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype(full))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if body:
            self.wfile.write(data)

    def do_GET(self):
        self._serve(True)

    def do_HEAD(self):
        self._serve(False)

    def _reject(self):
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _reject


def main():
    if not os.path.isdir(ROOT):
        print("WARNING: %s does not exist yet; requests will 404 until it does" % ROOT)
    print("Spelling: serving %s (index %s) read-only on :%d" % (ROOT, INDEX, PORT))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
