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
