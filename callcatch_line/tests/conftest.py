"""Fixtures: CallCatch Line in front of a real CallCatch 0.7.2 app (both in-process).

Fake data only.  Test tokens are obvious placeholders, never real secrets.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
from aiohttp.test_utils import TestClient, TestServer

LINE_APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
CC_APP = pathlib.Path(__file__).resolve().parents[2] / "voicemail" / "rootfs" / "app"

MCP_TOKEN = "test-mcp-token-" + "m" * 24
AGENT_TOKEN = "test-agent-token-" + "a" * 24
HOOK_TOKEN = "test-hook-token-" + "h" * 24


def _load(name: str, path: pathlib.Path, extra_path: pathlib.Path):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def line_mod():
    return _load("line_server", LINE_APP / "server.py", LINE_APP)


@pytest.fixture
def cc_mod(monkeypatch, tmp_path):
    opt = tmp_path / "cc_options.json"
    opt.write_text(json.dumps({
        "mock": True, "ingest_token": "ingest-token-555", "legacy_pyscript": "off",
        "storage_backend": "local", "log_level": "warning", "agent_token": AGENT_TOKEN,
        "push_service": "mobile_app_test_phone",
    }))
    monkeypatch.setenv("VM_OPTIONS", str(opt))
    monkeypatch.setenv("VM_MOCK", "1")
    monkeypatch.setenv("VM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VM_AUDIO_ROOT", str(tmp_path / "audio"))
    monkeypatch.setenv("VM_LEGACY_PATHS", str(tmp_path / "missing.json"))
    monkeypatch.setenv("VM_MIRROR_DIRS", str(tmp_path / "mirror"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)
    return _load("server", CC_APP / "server.py", CC_APP)


@pytest.fixture
async def stack(line_mod, cc_mod):
    """Yields (line_client, cc_app, cc_mod, make_line) with Line -> CallCatch over real HTTP."""
    cc_server = TestServer(cc_mod.make_app(), host="127.0.0.1")
    await cc_server.start_server()
    base = f"http://localhost:{cc_server.port}"
    clients = []

    async def make_line(**opts):
        options = {"mcp_token": MCP_TOKEN, "callcatch_agent_token": AGENT_TOKEN, "callcatch_url": base}
        options.update(opts)
        client = TestClient(TestServer(line_mod.make_app(options)))
        await client.start_server()
        clients.append(client)
        return client

    line = await make_line(hook_token=HOOK_TOKEN)
    try:
        yield line, cc_server.app, cc_mod, make_line
    finally:
        for c in clients:
            await c.close()
        await cc_server.close()
