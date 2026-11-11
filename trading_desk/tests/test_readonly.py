"""Read-only guard tests. Run: python3 tests/test_readonly.py"""
import asyncio
import pathlib
import shutil
import sys
import tempfile

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
sys.path.insert(0, str(APP))
import selfcheck  # noqa: E402

VERB = "PO" + "ST"  # built at runtime so this test file never trips the scan itself


def test_shipped_source_is_clean():
    assert selfcheck.scan(APP) == [], selfcheck.scan(APP)


def test_scan_catches_write_calls():
    bad = [
        "await session.post('https://api.alpaca.markets/v2/orders', json={})",
        "requests.delete(url + '/v2/orders/abc')",
        "client.submit_order(symbol='X')",
        "api.cancel_all_orders()",
        "api.close_position('X')",
        "fetch('/v2/orders', {method: '" + VERB + "'})",
        "app.router.add_" + "post('/x', h)",
    ]
    for line in bad:
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(APP, d, dirs_exist_ok=True)
            (pathlib.Path(d) / "evil.py").write_text(line + "\n")
            assert selfcheck.scan(pathlib.Path(d)), f"not caught: {line}"


def test_schwab_route_and_loop_are_get_only():
    text = (APP / "server.py").read_text(encoding="utf-8")
    assert 'add_get("/api/schwab"' in text
    start = text.index("async def schwab_loop")
    end = text.index("\nasync def ", start + 10)
    body = text[start:end]
    assert "rest_get" not in body  # the loop calls pull_schwab_market, which is the GET
    pull = text[text.index("async def pull_schwab_market"):text.index("async def schwab_loop")]
    assert 'rest_get(DATA_REST, "/v2/stocks/snapshots"' in pull
    assert 'rest_get(TRADE_REST, "/v2/clock")' in pull
    assert ".post" not in pull and "POST" not in pull and ".put" not in pull
    assert "write_text" not in text[text.index("def refresh_schwab_file"):text.index("def _mock_clock")]
    schwab_py = (APP / "schwab.py").read_text(encoding="utf-8")
    assert "write_text" not in schwab_py and "POST" not in schwab_py


def test_runtime_blocks_non_get():
    import os
    os.environ["TD_OPTIONS"] = "/nonexistent"
    import server
    import aiohttp

    async def go():
        s = server.make_session()
        try:
            await s.request(VERB, "https://example.invalid/v2/orders")
        except RuntimeError as e:
            return str(e)
        finally:
            await s.close()
        return None

    msg = asyncio.run(go())
    assert msg and "read-only" in msg, msg


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
