"""Schwab holdings file, price rules, and totals. Placeholder symbols only.

Run: python3 tests/test_schwab.py
"""
import asyncio
import copy
import io
import json
import logging
import math
import os
import pathlib
import sys
import tempfile

os.environ["TD_OPTIONS"] = "/nonexistent"
os.environ["TD_MOCK"] = "0"
os.environ.pop("TD_SCHWAB_FILE", None)

APP = pathlib.Path(__file__).resolve().parents[1] / "rootfs" / "app"
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import schwab  # noqa: E402
import server  # noqa: E402

NOW = 1_767_000_000_000
EXAMPLE = copy.deepcopy(schwab.MOCK_DOC)


def _desk():
    schwab.clear_cache()
    server.DESK = server.Desk()
    return server.DESK


def _view(trades=None, quotes=None, bars=None, clock=None, doc=None, reason="", keys=True):
    doc = EXAMPLE if doc is None else doc
    cleaned, why = schwab.clean(doc)
    assert cleaned, why
    clock = clock if clock is not None else {
        "is_open": True,
        "timestamp": "2026-01-02T15:00:00-05:00",
        "next_open": "2026-01-03T09:30:00-05:00",
        "next_close": "2026-01-02T16:00:00-05:00",
    }
    if trades is None:
        trades = {
            "AAA": {"stream": (110.0, NOW - 1000)},
            "BBB": {"stream": (20.0, NOW - 1000)},
        }
    if bars is None:
        bars = {
            "AAA": {"daily": (110.0, "2026-01-02T05:00:00Z"), "prev": (100.0, "2026-01-01T05:00:00Z")},
            "BBB": {"daily": (20.0, "2026-01-02T05:00:00Z"), "prev": (18.0, "2026-01-01T05:00:00Z")},
        }
    return schwab.build_view(cleaned, trades, quotes or {}, bars, clock, NOW, "iex", reason, keys)


def test_options_and_schema_bytes_unchanged():
    text = (ROOT / "config.yaml").read_text(encoding="utf-8")
    assert 'version: "0.3.5"' in text
    options, schema = text.split("options:\n", 1)[1].split("schema:\n", 1)
    assert options == (
        "  trading_env: live\n"
        "  data_feed: iex\n"
        "  data_stream: true\n"
        "  status_file: \"\"\n"
        "  status_entity: \"\"\n"
        "  mock: false\n"
        "  log_level: info\n"
    )
    assert schema == (
        "  api_key_id: password?\n"
        "  api_secret_key: password?\n"
        "  trading_env: list(live|paper)\n"
        "  data_feed: list(iex|sip|delayed_sip)\n"
        "  data_stream: bool\n"
        "  status_file: str?\n"
        "  status_entity: str?\n"
        "  feed_github_repo: str?\n"
        "  feed_github_path: str?\n"
        "  feed_github_token: password?\n"
        "  feed_github_poll_s: int(60,3600)?\n"
        "  trezor_btc_entity: str?\n"
        "  temp_open_risk_cap_pct: float?\n"
        "  single_name_cap_pct: float?\n"
        "  mock: bool\n"
        "  log_level: list(debug|info|warning|error)\n"
    )
    assert "schwab" not in schema
    assert "share:ro" in text
    assert schwab.PATH == "/share/trading_desk/schwab_holdings.json"
    assert schwab.holdings_path() == schwab.PATH


def test_validator_accepts_example_and_drops_unknown_keys():
    raw = copy.deepcopy(EXAMPLE)
    raw["secret_balance"] = 999999
    raw["accounts"][0]["account_number"] = "12345678"
    raw["accounts"][0]["positions"][0]["extra"] = {"qty": 99}
    doc, why = schwab.clean(raw)
    assert why == "" and doc["schema"] == schwab.SCHEMA
    assert "secret_balance" not in doc
    assert "account_number" not in doc["accounts"][0]
    assert "extra" not in doc["accounts"][0]["positions"][0]
    assert doc["accounts"][0]["positions"][0]["qty"] == 10
    naive = copy.deepcopy(EXAMPLE)
    naive["as_of"] = "2026-01-02T09:00:00"
    doc2, why2 = schwab.clean(naive)
    assert why2 == "" and doc2["as_of"].endswith("-05:00")


def _must_fail(raw, needle):
    doc, why = schwab.clean(raw)
    assert doc is None and why and needle in why, why
    blob = json.dumps({"ok": False, "error": why})
    assert "NaN" not in blob and "Infinity" not in blob


def test_validator_rejections():
    bad_symbol = copy.deepcopy(EXAMPLE)
    bad_symbol["accounts"][0]["positions"][0]["symbol"] = "aaa"
    _must_fail(bad_symbol, "bad symbol")
    for qty in (0, -1, float("nan"), float("inf")):
        raw = copy.deepcopy(EXAMPLE)
        raw["accounts"][0]["positions"][0]["qty"] = qty
        _must_fail(raw, "bad qty")
    raw = copy.deepcopy(EXAMPLE)
    raw["accounts"][1]["positions"][1]["static_price"] = None
    _must_fail(raw, "static row without price")
    raw = copy.deepcopy(EXAMPLE)
    raw["schema"] = "nope"
    _must_fail(raw, "wrong schema")
    raw = copy.deepcopy(EXAMPLE)
    raw["accounts"][0]["cash"] = float("nan")
    _must_fail(raw, "bad cash")
    raw = copy.deepcopy(EXAMPLE)
    raw["accounts"][0]["positions"][0]["cost_basis"] = float("inf")
    _must_fail(raw, "bad cost_basis")
    assert schwab.clean([])[0] is None
    assert schwab.clean({"schema": schwab.SCHEMA})[0] is None


def test_load_file_cases():
    schwab.clear_cache()
    missing = schwab.load_file("/tmp/trading-desk-no-such-holdings.json")
    assert missing == {"ok": False, "error": "no holdings file", "holdings": None, "key": None} or (
        missing["ok"] is False and missing["error"] == "no holdings file")
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "schwab_holdings.json"
        path.write_text("{", encoding="utf-8")
        bad = schwab.load_file(path)
        assert bad["ok"] is False and bad["error"] == "bad JSON"
        path.write_text(json.dumps({"schema": "other", "as_of": "2026-01-02T09:00:00-05:00", "accounts": []}), encoding="utf-8")
        schwab.clear_cache()
        wrong = schwab.load_file(path)
        assert wrong["ok"] is False and "schema" in wrong["error"]
        path.write_text(json.dumps(EXAMPLE), encoding="utf-8")
        schwab.clear_cache()
        good = schwab.load_file(path)
        assert good["ok"] is True and good["holdings"]["accounts"][0]["id"] == "individual"
        again = schwab.load_file(path)
        assert again["holdings"] is good["holdings"]
        path.write_bytes(b"{" + b"x" * (schwab.MAX_BYTES + 5))
        schwab.clear_cache()
        huge = schwab.load_file(path)
        assert huge["ok"] is False and huge["error"] == "holdings file too large"
    # a thrown validator still returns ok:false
    assert schwab.clean(None)[0] is None


def test_price_precedence_and_prev_close():
    mid, ts, src = schwab.choose_price(None, None, None, {"bp": 10, "ap": 12, "ts": 50})
    assert src == "alpaca quote mid" and mid == 11 and ts == 50
    assert schwab.choose_price(None, None, None, {"bp": 10, "ap": None, "ts": 1})[2] == "none"
    px, _ts, src = schwab.choose_price((5, 300), (9, 100), (8, 200), {"bp": 1, "ap": 3, "ts": 999})
    assert src == "alpaca trade" and px == 5  # stream is newest
    px, _ts, src = schwab.choose_price((5, 100), (9, 150), (8, 400), None)
    assert px == 8 and src == "alpaca trade"  # snapshot trade is newest
    assert schwab.choose_price(None, None, None, None) == (None, None, "none")

    def clock(open_, ts, nxt):
        return {"is_open": open_, "timestamp": ts, "next_open": nxt, "next_close": nxt}

    daily = (110.0, "2026-01-02T05:00:00Z")
    prev = (100.0, "2026-01-01T05:00:00Z")
    # market open
    assert schwab.prev_close(daily, prev, clock(True, "2026-01-02T15:00:00-05:00", "2026-01-03T09:30:00-05:00")) == 100
    # after close: daily bar is today, so previous session close
    assert schwab.prev_close(daily, prev, clock(False, "2026-01-02T17:00:00-05:00", "2026-01-03T09:30:00-05:00")) == 100
    # pre-market next day: next open is today, use the last daily bar
    friday = (100.0, "2026-01-02T05:00:00Z")
    assert schwab.prev_close(friday, (99.0, "2026-01-01T05:00:00Z"),
                             clock(False, "2026-01-05T08:00:00-05:00", "2026-01-05T09:30:00-05:00")) == 100
    # weekend: daily bar is Friday, not Saturday
    assert schwab.prev_close(friday, prev, clock(False, "2026-01-03T12:00:00-05:00", "2026-01-05T09:30:00-05:00")) == 100
    # holiday, clock closed, daily bar is not that date
    assert schwab.prev_close(friday, prev, clock(False, "2026-01-01T12:00:00-05:00", "2026-01-02T09:30:00-05:00")) == 100
    assert schwab.prev_close(None, None, clock(True, "2026-01-02T15:00:00-05:00", "2026-01-03T09:30:00-05:00")) is None


def test_math_to_the_cent_and_exclusions():
    view = _view()
    ind, roth = view["accounts"]
    aaa, bbb = ind["positions"]
    assert round(aaa["value"], 2) == 1100.00
    assert round(aaa["day_pl"], 2) == 100.00
    assert round(aaa["unrealized_pl"], 2) == 100.00
    assert abs(aaa["unrealized_plpc"] - 10) < 1e-9
    assert aaa["label"] == "live"
    assert round(bbb["value"], 2) == 110.00
    assert round(bbb["day_pl"], 2) == 11.00
    assert bbb["unrealized_pl"] is None and bbb["cost_basis"] is None
    assert round(ind["positions_value"], 2) == 1210.00
    assert round(ind["total"], 2) == 1310.00
    assert round(ind["day_pl"], 2) == 111.00
    # total - day_pl - excluded = 1310 - 111 - 0
    assert abs(ind["day_plpc"] - (111 / 1199 * 100)) < 1e-9
    assert ind["basis_excluded"] == ["BBB"]
    fund = roth["positions"][1]
    assert fund["symbol"] == "FUNDX" and fund["price"] == 50 and fund["day_pl"] is None
    assert fund["price_source"] == "snapshot" and fund["label"].startswith("snapshot ") and fund["label"].endswith("ET")
    assert "FUNDX" in roth["day_excluded"] and "FUNDX" in roth["basis_excluded"]
    assert round(roth["total"], 2) == 395.00
    assert round(roth["day_pl"], 2) == 20.00
    # 395 - 20 - 150
    assert abs(roth["day_plpc"] - (20 / 225 * 100)) < 1e-9
    assert round(roth["unrealized_pl"], 2) == 70.00
    combo = view["combined"]
    assert round(combo["total"], 2) == 1705.00
    assert round(combo["day_pl"], 2) == 131.00
    assert round(combo["cash"], 2) == 125.00
    # 1705 - 131 - 150
    assert abs(combo["day_plpc"] - (131 / 1424 * 100)) < 1e-9
    assert round(combo["unrealized_pl"], 2) == 170.00
    assert abs(combo["unrealized_plpc"] - (170 / 1150 * 100)) < 1e-9
    assert "FUNDX" in combo["day_excluded"]
    blob = json.dumps(view)
    assert "NaN" not in blob and "Infinity" not in blob and "undefined" not in blob


def test_missing_symbol_empty_account_and_no_keys():
    trades = {"AAA": {"stream": (110.0, NOW - 1000)}}
    bars = {"AAA": {"daily": (110.0, "2026-01-02T05:00:00Z"), "prev": (100.0, "2026-01-01T05:00:00Z")}}
    view = _view(trades=trades, bars=bars, reason="data stream error 401: no", keys=True)
    ind = view["accounts"][0]
    assert "BBB" in ind["missing"] and ind["positions"][1]["value"] is None
    assert ind["positions"][1]["label"] == "no quote"
    assert round(ind["total"], 2) == 1200.00  # AAA 1100 + cash 100, BBB left out
    assert "BBB" not in [p["symbol"] for p in ind["positions"] if p["value"] is not None]
    doc = copy.deepcopy(EXAMPLE)
    doc["accounts"] = [{"id": "cashonly", "name": "Cash", "cash": 25, "positions": []}]
    empty = _view(doc=doc, trades={}, bars={}, keys=True)
    assert empty["ok"] and empty["accounts"][0]["positions"] == []
    assert empty["combined"]["total"] == 25
    bare = _view(trades={}, bars={}, quotes={}, keys=False, reason="Add API key and secret in the add-on Configuration tab")
    assert bare["prices_note"].startswith("No live prices")
    assert "Configuration" in bare["prices_note"]
    assert bare["accounts"][0]["positions"][0]["qty"] == 10  # shares still listed
    assert bare["accounts"][1]["positions"][1]["price"] == 50  # static fund still priced
    blob = json.dumps(bare) + json.dumps(empty) + json.dumps(view)
    assert "NaN" not in blob and "Infinity" not in blob


def test_labels_and_market_clock():
    closed = _view(clock={
        "is_open": False,
        "timestamp": "2026-01-02T17:00:00-05:00",
        "next_open": "2026-01-03T09:30:00-05:00",
        "next_close": "2026-01-03T16:00:00-05:00",
    })
    label = closed["accounts"][0]["positions"][0]["label"]
    assert label.startswith("closed · last trade ") and label.endswith("ET")
    assert closed["market"]["is_open"] is False
    assert closed["market"]["next_open"].startswith("2026-01-03T09:30:00")
    stale = _view(trades={"AAA": {"stream": (110.0, NOW - 180_000)}, "BBB": {"stream": (20.0, NOW - 1000)}})
    assert stale["accounts"][0]["positions"][0]["label"] == "delayed"
    quoted = _view(trades={}, quotes={"AAA": {"bp": 109, "ap": 111, "ts": NOW - 1000},
                                      "BBB": {"bp": 19, "ap": 21, "ts": NOW - 1000}})
    row = quoted["accounts"][0]["positions"][0]
    assert row["price_source"] == "alpaca quote mid" and row["price"] == 110
    assert row["label"] == "delayed"  # a quote is not a fresh trade


def test_fetch_gate_and_paths():
    gate = schwab.FetchGate()
    assert gate.due(0, False) is True
    assert gate.due(30, False) is False
    assert gate.due(30, True) is True
    assert gate.due(31, False) is False
    assert gate.due(91, False) is True
    text = (APP / "server.py").read_text(encoding="utf-8")
    assert "watch_symbols()" in text
    assert "DESK.watch_symbols()" in text[text.index("async def price_poll_loop"):text.index("def note_trade")]
    stream = text[text.index("async def data_stream"):text.index("async def github_feed")]
    assert "watch_symbols()" in stream
    assert "https://" not in (APP / "schwab.py").read_text(encoding="utf-8")


def test_pull_uses_only_snapshots_and_clock():
    desk = _desk()
    doc, _ = schwab.clean(EXAMPLE)
    desk.schwab_doc = doc
    calls = []

    async def fake(base, path, params=None):
        calls.append((base, path, params))
        if path == "/v2/stocks/snapshots":
            return {"AAA": {"latestTrade": {"p": 110, "t": "2026-01-02T15:00:00Z"},
                            "latestQuote": {"bp": 109.5, "ap": 110.5, "t": "2026-01-02T15:00:00Z"},
                            "dailyBar": {"c": 110, "t": "2026-01-02T05:00:00Z"},
                            "prevDailyBar": {"c": 100, "t": "2026-01-01T05:00:00Z"}},
                    "BBB": {"latestTrade": {"p": 20, "t": "2026-01-02T15:00:00Z"},
                            "dailyBar": {"c": 20, "t": "2026-01-02T05:00:00Z"},
                            "prevDailyBar": {"c": 18, "t": "2026-01-01T05:00:00Z"}}}
        if path == "/v2/clock":
            return {"is_open": True, "next_open": "2026-01-03T09:30:00-05:00",
                    "next_close": "2026-01-02T16:00:00-05:00", "timestamp": "2026-01-02T15:00:00-05:00"}
        raise AssertionError(path)

    server.rest_get = fake
    asyncio.run(server.pull_schwab_market())
    assert [c[1] for c in calls] == ["/v2/stocks/snapshots", "/v2/clock"]
    assert calls[0][0] == server.DATA_REST and calls[1][0] == server.TRADE_REST
    assert calls[0][2]["feed"] == server.FEED
    assert calls[0][2]["symbols"] == "AAA,BBB"
    assert desk.clock["is_open"] is True
    assert desk.trades["AAA"]["snap"][0] == 110
    assert "FUNDX" not in calls[0][2]["symbols"]


def test_prune_keeps_schwab_symbols_and_file_change_unsubscribes():
    desk = _desk()
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "schwab_holdings.json"
        path.write_text(json.dumps(EXAMPLE), encoding="utf-8")
        os.environ["TD_SCHWAB_FILE"] = str(path)
        schwab.clear_cache()
        try:
            assert server.refresh_schwab_file() is True
            desk.positions = [{"symbol": "ZZZ"}]
            desk.live_prices = {"AAA": (110, 1), "BBB": (20, 1), "ZZZ": (1, 1), "OLD": (9, 1)}
            desk.trades = {"OLD": {"stream": (9, 1)}, "AAA": {"stream": (110, 1)}}
            previous = list(desk.watch_symbols())
            assert "BBB" in previous and "AAA" in previous
            server.prune_live_prices(previous)
            assert "OLD" not in desk.live_prices and "OLD" not in desk.trades
            assert "AAA" in desk.live_prices and "BBB" in desk.live_prices
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc["accounts"][0]["positions"] = [p for p in doc["accounts"][0]["positions"] if p["symbol"] != "BBB"]
            path.write_text(json.dumps(doc), encoding="utf-8")
            schwab.clear_cache()
            before = list(desk.watch_symbols())
            desk.symbols_changed.clear()
            server.prune_live_prices(before)
            assert "BBB" not in desk.watch_symbols()
            assert "BBB" not in desk.live_prices
            assert "AAA" in desk.live_prices
            assert desk.symbols_changed.is_set()
        finally:
            os.environ.pop("TD_SCHWAB_FILE", None)
            schwab.clear_cache()


def test_totals_follow_the_file_and_fall_back():
    desk = _desk()
    doc, _ = schwab.clean(EXAMPLE)
    desk.schwab_doc = doc
    desk.trades = {"AAA": {"stream": (110.0, NOW)}, "BBB": {"stream": (20.0, NOW)}}
    desk.bars = {
        "AAA": {"daily": (110.0, "2026-01-02T05:00:00Z"), "prev": (100.0, "2026-01-01T05:00:00Z")},
        "BBB": {"daily": (20.0, "2026-01-02T05:00:00Z"), "prev": (18.0, "2026-01-01T05:00:00Z")},
    }
    desk.clock = {"is_open": True, "timestamp": "2026-01-02T15:00:00-05:00",
                  "next_open": "2026-01-03T09:30:00-05:00", "next_close": "2026-01-02T16:00:00-05:00"}
    snap = desk.snapshot()
    view = snap["schwab"]
    totals = snap["totals"]
    assert view["ok"] is True
    assert totals["schwab_source"] == "holdings file + Alpaca live"
    assert totals["schwab_total"] == view["combined"]["total"]
    assert totals["day"]["schwab_pl"] == view["combined"]["day_pl"]
    assert totals["unrealized"]["schwab_pl"] == view["combined"]["unrealized_pl"]
    body = json.loads(asyncio.run(server.api_schwab(None)).body)
    assert round(body["combined"]["total"], 2) == round(view["combined"]["total"], 2)
    desk.schwab_doc = None
    desk.schwab_error = "no holdings file"
    fallback = desk.totals(1000, 10, {"schwab_total": 42000, "schwab_day_pl": 5,
                                      "schwab_day_plpc": 1, "schwab_unrealized_pl": 7,
                                      "source": "desk feed", "as_of": "2026-01-02T09:00:00-05:00"})
    assert fallback["schwab_total"] == 42000
    assert fallback["schwab_source"] == "desk feed"
    assert fallback["day"]["schwab_pl"] == 5


def test_logs_counts_only_and_redact_errors():
    desk = _desk()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.addFilter(server.Redact())
    server.log.addHandler(handler)
    server.log.setLevel(logging.DEBUG)
    secret = "dummytok_0000000000000000ABCD"
    server.GH_TOKEN = secret
    try:
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "schwab_holdings.json"
            path.write_text(json.dumps(EXAMPLE), encoding="utf-8")
            os.environ["TD_SCHWAB_FILE"] = str(path)
            schwab.clear_cache()
            server.refresh_schwab_file()
            desk.schwab_doc = schwab.clean(EXAMPLE)[0]

            async def boom(*_a, **_k):
                raise RuntimeError("snapshot failed for " + secret)

            server.rest_get = boom
            asyncio.run(server.pull_schwab_market())
        text = buf.getvalue()
        assert secret not in text
        assert "****ABCD" in text
        assert "FUNDX" not in text and "1000" not in text and "0000" not in text
        assert "accounts=" in text or "accounts" in text
    finally:
        server.log.removeHandler(handler)
        os.environ.pop("TD_SCHWAB_FILE", None)
        server.GH_TOKEN = ""
        schwab.clear_cache()


def test_gate_math_is_finite():
    view = _view()
    def walk(obj):
        if isinstance(obj, float):
            assert math.isfinite(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(view)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
