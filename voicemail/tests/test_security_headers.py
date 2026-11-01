"""Browser hardening headers and diag redaction (0.3.2). Fake data only."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

from aiohttp.test_utils import TestClient, TestServer

ROOT = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
STATIC = ROOT / "static"
sys.path.insert(0, str(ROOT))

INGRESS = {"X-Remote-User-Id": "user-555"}


def _load(monkeypatch, tmp_path):
    opt = tmp_path / "options.json"
    opt.write_text(
        json.dumps({"mock": True, "ingest_token": "ingest-token-555", "legacy_pyscript": "off",
                    "storage_backend": "local", "media_player": "", "log_level": "warning"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("VM_OPTIONS", str(opt))
    monkeypatch.setenv("VM_MOCK", "1")
    monkeypatch.setenv("VM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VM_AUDIO_ROOT", str(tmp_path / "audio"))
    monkeypatch.setenv("VM_LEGACY_PATHS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("VM_MIRROR_DIRS", str(tmp_path / "mirror"))
    monkeypatch.setenv("VM_INGRESS_IP", "127.0.0.1")
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    for name in list(sys.modules):
        if name == "server":
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("server", ROOT / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["server"] = mod
    spec.loader.exec_module(mod)
    return mod


def _assert_hardened(resp):
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "frame-ancestors 'self'" in csp
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"


async def test_headers_on_html_static_api_and_errors(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        for path in ("/", "/app.js", "/style.css", "/api/health"):
            resp = await client.get(path)
            assert resp.status == 200, path
            _assert_hardened(resp)
        resp = await client.get("/api/voicemails", headers=INGRESS)
        assert resp.status == 200
        _assert_hardened(resp)
        assert "no-store" in resp.headers.get("Cache-Control", "")
        # Refused and missing routes are hardened too.
        resp = await client.get("/api/voicemails")
        assert resp.status == 403
        _assert_hardened(resp)
        resp = await client.get("/nope-555.html")
        assert resp.status == 404
        _assert_hardened(resp)


async def test_diag_does_not_describe_the_supervisor_token(tmp_path, monkeypatch):
    mod = _load(monkeypatch, tmp_path)
    async with TestClient(TestServer(mod.make_app())) as client:
        resp = await client.get("/api/diag", headers=INGRESS)
        assert resp.status == 200
        body = await resp.json()
        assert "token_len" not in body and "token_source" not in body
        assert "ingest-token-555" not in json.dumps(body)
        assert body["dry_run"] == {
            "work_forward_dry_run": "unknown", "push_owner": "unknown", "blueprint_default_input": "on",
        }


def test_ui_has_no_inline_script_or_handlers():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script> breaks the CSP"
    assert "<style" not in html
    for text in (html, js):
        assert not re.search(r"\son[a-z]+\s*=\s*\"", text), "inline event handler breaks the CSP"
        assert not re.search(r"\sstyle\s*=\s*\"", text), "inline style attribute breaks the CSP"


def test_crash_500_has_security_headers(monkeypatch, tmp_path):
    import asyncio

    from aiohttp import web

    mod = _load(monkeypatch, tmp_path)

    async def boom(_request):
        raise RuntimeError("secret detail")

    async def run():
        app = web.Application(middlewares=[mod.security_headers])
        app.router.add_get("/api/boom", boom)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/boom")
            body = await resp.text()
            assert resp.status == 500
            assert "secret detail" not in body
            for key in mod.SECURITY_HEADERS:
                assert key in resp.headers
            assert resp.headers.get("Cache-Control") == "private, no-store"

    asyncio.run(run())


def test_panel_admin_default_admin_only():
    # Supervisor defaults panel_admin to true; the linter rejects the explicit default.
    cfg = (pathlib.Path(__file__).resolve().parents[1] / "config.yaml").read_text()
    assert "panel_admin" not in cfg
