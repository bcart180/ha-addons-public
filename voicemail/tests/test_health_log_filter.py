"""0.4.2: the access-log filter drops only successful exact GET /api/health lines. Fake data only."""
from __future__ import annotations

import logging

from test_security_headers import ROOT, _load


def _keep(mod, msg):
    rec = logging.LogRecord("aiohttp.access", logging.INFO, "", 0, msg, None, None)
    return mod.NoHealthAccess().filter(rec)


def test_successful_health_dropped(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for msg in ('172.30.32.2 [01/Nov/2026:16:00:00 +0000] "GET /api/health HTTP/1.1" 200 3 "-" "HealthCheck"',
                '127.0.0.1 [..] "GET /api/health HTTP/1.0" 204 0 "-" "x"'):
        assert _keep(mod, msg) is False, msg


def test_other_lines_still_log(monkeypatch, tmp_path):
    mod = _load(monkeypatch, tmp_path)
    for msg in ('172.30.32.2 [..] "GET /api/health HTTP/1.1" 500 10 "-" "x"',
                '172.30.32.2 [..] "GET /api/health HTTP/1.1" 404 10 "-" "x"',
                '172.30.32.2 [..] "GET /api/healthz?x HTTP/1.1" 200 3 "-" "x"',
                '172.30.32.2 [..] "GET /api/health?x=1 HTTP/1.1" 200 3 "-" "x"',
                '172.30.32.2 [..] "GET /api/healthz HTTP/1.1" 200 3 "-" "x"',
                '172.30.32.2 [..] "POST /api/health HTTP/1.1" 200 3 "-" "x"',
                '172.30.32.2 [..] "GET /api/voicemails?q=health HTTP/1.1" 200 3 "-" "x"',
                '172.30.32.2 [..] "GET /api/health/extra HTTP/1.1" 200 3 "-" "x"',
                "health check failed: upstream timeout"):
        assert _keep(mod, msg) is True, msg


def test_filter_wired_in_main():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    body = src.split("def main() -> None:", 1)[1]
    assert 'logging.getLogger("aiohttp.access").addFilter(NoHealthAccess())' in body
