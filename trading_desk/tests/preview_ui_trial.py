#!/usr/bin/env python3
"""Local synthetic preview of /ui-trial. No broker, no secrets, GET only.

    python3 tests/preview_ui_trial.py

Then open http://127.0.0.1:8765/ui-trial
Numbers are invented sample names (ALPHA, BRAVO, …), not an account.
"""
import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "rootfs" / "app" / "static"
HOST, PORT = "127.0.0.1", 8765

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
NOW_MS = int(NOW.timestamp() * 1000)


def _series(step, n, base, end):
    """Rising sample curve so a gain paints in the up color, with a small wobble."""
    t, e = [], []
    start = NOW.timestamp() - (n - 1) * step
    for i in range(n):
        wobble = ((i % 4) - 1.5) * abs(end - base) * 0.05
        v = base + (end - base) * (i / max(1, n - 1)) + wobble
        t.append(int(start + i * step))
        e.append(round(v, 2))
    e[-1] = float(end)
    return t, e


def state():
    curve_t, curve_e = _series(300, 24, 14100, 14390)
    return {
        "v": "0.3.3", "ts_ms": NOW_MS, "mock": True, "env": "paper",
        "keys_configured": True,
        "health": {
            "trade_stream": {"state": "streaming", "last_msg_ms": NOW_MS, "error": None},
            "data_stream": {"state": "connecting", "last_msg_ms": NOW_MS - 5000, "error": None, "feed": "iex"},
            "rest": {"last_ok_ms": NOW_MS, "error": None},
            "status_feed": {"source": "sample", "error": None, "example": False},
        },
        "account": {
            "equity": 14390.0, "last_equity": 14100.0, "day_pl": 290.0, "day_plpc": 2.06,
            "cash": 4200.0, "buying_power": 4200.0, "long_market_value": 10190.0,
            "status": "ACTIVE", "trading_blocked": False,
        },
        "positions": [
            {"symbol": "ALPHA", "qty": 40, "side": "long", "avg_entry": 150, "price": 162, "price_live": True,
             "market_value": 6480, "unrealized_pl": 480, "unrealized_plpc": 8.0, "day_change_pct": 1.2},
            {"symbol": "BRAVO", "qty": 25, "side": "long", "avg_entry": 80, "price": 74, "price_live": True,
             "market_value": 1850, "unrealized_pl": -150, "unrealized_plpc": -7.5, "day_change_pct": -2.4},
            {"symbol": "CIDER", "qty": 15, "side": "long", "avg_entry": 40, "price": 44, "price_live": False,
             "market_value": 660, "unrealized_pl": 60, "unrealized_plpc": 10.0, "day_change_pct": 0.8},
            {"symbol": "DELTA", "qty": 10, "side": "long", "avg_entry": 120, "price": 120, "price_live": False,
             "market_value": 1200, "unrealized_pl": 0, "unrealized_plpc": 0, "day_change_pct": 0},
        ],
        "orders": [
            {"symbol": "ALPHA", "side": "sell", "type": "stop", "qty": 40, "stop_price": 140, "limit_price": None, "status": "new"},
            {"symbol": "BRAVO", "side": "sell", "type": "limit", "qty": 10, "stop_price": None, "limit_price": 78, "status": "accepted"},
        ],
        "fills": [
            {"symbol": "CIDER", "side": "buy", "qty": 15, "price": 40, "time": (NOW - dt.timedelta(minutes=55)).isoformat()},
            {"symbol": "ALPHA", "side": "buy", "qty": 10, "price": 150, "time": (NOW - dt.timedelta(minutes=25)).isoformat()},
        ],
        "curve": {"t": curve_t, "equity": curve_e, "base": 14100},
        "risk": {
            "open_risk_usd": 180.0, "open_risk_pct": 1.25, "desk_open_risk_pct": 1.1,
            "unprotected": [{"symbol": "DELTA", "qty": 10}],
            "largest": {"symbol": "ALPHA", "weight_pct": 45.0, "risk_usd": 120},
            "caps": {
                "temp_open_risk_pct": {"value": 20, "source": "desk status feed"},
                "single_name_cap_pct": {"value": 50, "source": "desk status feed"},
            },
        },
        "desk": {
            "example": False, "feed": "live", "updated": NOW.isoformat(),
            "source": "sample", "error": None,
            "bots": [
                {"name": "Desk Lead", "status": "working", "last_action": "Sample: routed a note to Risk", "ts": NOW.isoformat(), "derived": False},
                {"name": "Strategist", "status": "idle", "last_action": "Sample: rules check passed", "ts": NOW.isoformat()},
                {"name": "Risk Manager", "status": "working", "last_action": "Sample: sizing a 0.5% idea", "ts": NOW.isoformat()},
                {"name": "Research Analyst", "status": "idle", "last_action": "Sample: wrote a brief", "ts": NOW.isoformat()},
                {"name": "Execution Trader", "status": "waiting", "last_action": "Sample: waiting on the risk note", "ts": NOW.isoformat()},
                {"name": "Trade Reviewer", "status": "idle", "last_action": "Sample: read yesterday's fills", "ts": NOW.isoformat()},
                {"name": "Trading Floor", "status": "alert", "last_action": "Sample: digest posted", "ts": NOW.isoformat()},
            ],
            "pipeline": [
                {"trade_id": "SAMPLE-1", "symbol": "ALPHA", "stage": "scan", "ts": (NOW - dt.timedelta(minutes=30)).isoformat(), "bot": "Strategist"},
                {"trade_id": "SAMPLE-1", "symbol": "ALPHA", "stage": "vet", "ts": (NOW - dt.timedelta(minutes=20)).isoformat(), "bot": "Research Analyst"},
                {"trade_id": "SAMPLE-1", "symbol": "ALPHA", "stage": "size", "ts": (NOW - dt.timedelta(minutes=10)).isoformat(), "bot": "Risk Manager"},
                {"trade_id": "SAMPLE-2", "symbol": "CIDER", "stage": "fill", "ts": (NOW - dt.timedelta(minutes=55)).isoformat(), "bot": "Execution Trader"},
            ],
            "proposals": [
                {"id": "SAMPLE-R1", "out": "DELTA", "in": "ALPHA", "reason": "Sample rotation screen. Not an order.", "score": 0.62, "status": "proposed", "ts": NOW.isoformat()},
                {"id": "SAMPLE-R2", "out": "BRAVO", "in": "CIDER", "reason": "Sample relative-strength note.", "score": 0.41, "status": "watch", "ts": NOW.isoformat()},
            ],
            "thresholds": [
                {"name": "Max sample risk", "current": 0.4, "limit": 1.0, "unit": "%"},
                {"name": "Sample floor", "value": "2 names"},
            ],
            "holdings": {
                "schwab_total": 42000, "schwab_unrealized_pl": 1800, "schwab_day_pl": 140, "schwab_day_plpc": 0.33,
                "alpaca_equity": 14390, "combined_total": 56390, "schwab_unrealized_vs_cost": 1800,
                "combined_alltime_pl": 2400, "as_of": NOW.isoformat(), "price_as_of": NOW.isoformat(),
                "accounts": [{
                    "name": "Sample long-term",
                    "cash": 500,
                    "positions": [
                        {"symbol": "ECHO", "name": "Echo Sample Fund", "qty": 20, "price": 50, "prev_close": 48,
                         "value": 1000, "cost_basis": 800, "cost_per_share": 40, "day_pl": 40, "day_plpc": 4.17,
                         "unrealized_pl": 200, "unrealized_plpc": 25, "price_source": "snapshot", "core": True},
                        {"symbol": "FOXTROT", "name": "Foxtrot Sample", "qty": 8, "price": 50, "prev_close": 51,
                         "value": 400, "cost_basis": None, "cost_per_share": None, "day_pl": -8, "day_plpc": -1.96,
                         "unrealized_pl": None, "unrealized_plpc": None, "price_source": "snapshot"},
                    ],
                }],
                "alpaca_alltime": {
                    "first_activity": "2024-01-02", "net_deposits": 10000,
                    "total_pl_since_inception": 600, "total_pl_since_inception_pct": 6,
                    "realized_complete": True, "realized_pl_fifo": 120, "realized_by_symbol": {"CIDER": 40, "ALPHA": 80},
                    "unrealized_pl": 390, "other_pl": 90,
                },
            },
            "portfolio": {
                "equity": 14390, "day_pl": 290, "day_plpc": 2.06, "lifetime_pl": 600, "lifetime_plpc": 4.35,
                "since": "2024-01-02", "net_deposits": 10000, "cash": 4200, "updated": NOW.isoformat(),
                "positions": [
                    {"symbol": "ALPHA", "qty": 40, "price": 162, "value": 6480, "weight_pct": 45.0, "day_pl": 80, "day_plpc": 1.2, "lifetime_pl": 480, "lifetime_plpc": 8},
                    {"symbol": "BRAVO", "qty": 25, "price": 74, "value": 1850, "weight_pct": 12.9, "day_pl": -40, "day_plpc": -2.4, "lifetime_pl": -150, "lifetime_plpc": -7.5},
                    {"symbol": "CIDER", "qty": 15, "price": 44, "value": 660, "weight_pct": 4.6, "day_pl": 10, "day_plpc": 0.8, "lifetime_pl": 60, "lifetime_plpc": 10},
                    {"symbol": "DELTA", "qty": 10, "price": 120, "value": 1200, "weight_pct": 8.3, "day_pl": 0, "day_plpc": 0, "lifetime_pl": 0, "lifetime_plpc": 0},
                ],
            },
        },
        "totals": {
            "combined_total": 56390, "schwab_total": 42000, "alpaca_equity": 14390,
            "day": {"combined_pl": 430, "alpaca_pl": 290, "alpaca_plpc": 2.06, "schwab_pl": 140, "schwab_plpc": 0.33},
            "week": {"alpaca_pl": 1200, "alpaca_plpc": 9.1, "scope": "alpaca"},
            "month": {"alpaca_pl": 800, "alpaca_plpc": 5.9, "scope": "alpaca"},
            "year": {"alpaca_pl": None, "alpaca_plpc": None, "scope": "alpaca"},
            "unrealized": {"combined_pl": 2190, "alpaca_pl": 390, "schwab_pl": 1800},
            "schwab_as_of": NOW.isoformat(), "schwab_price_as_of": NOW.isoformat(),
        },
        "crypto": {
            "btc_qty": 0.15, "btc_price": 60000, "btc_open_24h": 58000, "value": 9000,
            "day_pl": 300, "day_plpc": 3.45, "price_source": "Sample public spot",
            "price_as_of_ms": NOW_MS, "qty_error": None, "price_error": None,
            "qty_source": "input_number.sample_btc",
        },
        "ha_prices": {},
        "ranges": {
            "1W": {"pl": 1200, "plpc": 9.1, "covers_from": int(NOW.timestamp()) - 8 * 86400, "scope": "alpaca", "error": None},
            "1M": {"pl": 800, "plpc": 5.9, "covers_from": int(NOW.timestamp()) - 32 * 86400, "scope": "alpaca", "error": None},
            "5A": {"pl": 5400, "plpc": 60, "covers_from": int(NOW.timestamp()) - 400 * 86400, "scope": "alpaca", "error": None},
        },
        "gauges": [
            {"name": "Open risk", "value": 1.25, "limit": 20, "unit": "%", "source": "desk status feed"},
            {"name": "Largest name (ALPHA)", "value": 45, "limit": 50, "unit": "%", "source": "desk status feed"},
            {"name": "Unprotected positions (no resting stop)", "value": 1, "limit": 0, "unit": "", "source": "sample open orders", "zero_ok": True},
            {"name": "Max sample risk", "value": 0.4, "limit": 1, "unit": "%", "source": "desk status feed"},
            {"name": "Day P&L", "value": 2.06, "limit": None, "unit": "%", "source": "sample account", "info": True},
        ],
    }


def history(key):
    spans = {"1W": (35, 3600), "1M": (22, 86400), "1A": (0, 86400), "5A": (60, 86400), "1D": (24, 300)}
    n, step = spans.get(key, (0, 86400))
    if n < 2:
        return {"range": key, "error": "not loaded yet", "scope": "alpaca", "t": [], "equity": []}
    t, e = _series(step, n, 13000, 14390)
    return {"range": key, "t": t, "equity": e, "pl": round(e[-1] - e[0], 2), "scope": "alpaca", "error": None}


def bars(sym):
    if sym not in {"ALPHA", "BRAVO", "CIDER", "DELTA"}:
        return 400, {"error": "symbol not held"}
    px = {"ALPHA": 162, "BRAVO": 74, "CIDER": 44, "DELTA": 120}[sym]
    out, t0 = [], int(NOW.timestamp()) - 40 * 300
    for i in range(40):
        o = px - 2 + (i % 6) * 0.4
        c = o + (0.6 if i % 2 == 0 else -0.4)
        out.append({"t": t0 + i * 300, "o": round(o, 2), "h": round(max(o, c) + 0.3, 2),
                    "l": round(min(o, c) - 0.3, 2), "c": round(c, 2), "vw": round((o + c) / 2, 2)})
    entry = {"ALPHA": 150, "BRAVO": 80, "CIDER": 40, "DELTA": 120}[sym]
    stops = [140] if sym == "ALPHA" else []
    return 200, {"symbol": sym, "bars": out, "entry": entry, "stops": stops}


def assets():
    as_of = (NOW - dt.timedelta(hours=8)).isoformat()
    return {"ok": True, "error": None, "assets": {
        "as_of": as_of, "source": "sample file", "account": "Sample brokerage", "stale": True,
        "account_value": 42000, "day_change": 140, "day_change_pct": 0.33,
        "unrealized": 1800, "unrealized_pct": 4.48,
        "positions": [
            {"symbol": "ECHO", "name": "Echo Sample Fund", "qty": 20, "price": 50, "price_chg": 2,
             "mkt_val": 1000, "day_chg": 40, "day_pct": 4.17, "gain": 200, "gain_pct": 25},
            {"symbol": "FOXTROT", "name": "Foxtrot Sample", "qty": 8, "price": 50, "price_chg": -1,
             "mkt_val": 400, "day_chg": -8, "day_pct": -1.96, "gain": 0, "gain_pct": 0},
        ],
        "equities_total": {"mkt_val": 1400, "day_chg": 32, "day_pct": 2.34, "gain": 200, "gain_pct": 16.7, "alloc_pct": 3.3},
        "cash": {"value": 40600, "alloc_pct": 96.7},
    }}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.command, self.path))

    def do_GET(self):  # noqa: N802 — stdlib handler name
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/ui-trial"):
            body = (STATIC / "ui-trial" / "index.html").read_bytes()
            return self._send(200, "text/html; charset=utf-8", body, "no-cache")
        if path == "/ui-trial/":
            return self._send(302, "text/plain", b"see /\n", "no-store", {"Location": "../"})
        if path == "/legacy":
            body = (STATIC / "index.html").read_bytes()
            return self._send(200, "text/html; charset=utf-8", body, "no-cache")
        if path == "/legacy/":
            return self._send(302, "text/plain", b"see legacy\n", "no-store", {"Location": "../legacy"})
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
                return self._send(404, "text/plain", b"missing\n", "no-store")
            kind = "text/css" if target.suffix == ".css" else "text/javascript" if target.suffix == ".js" else "application/octet-stream"
            if target.suffix == ".html":
                kind = "text/html; charset=utf-8"
            return self._send(200, kind, target.read_bytes(), "no-cache")
        if path == "/api/state":
            return self._json(state())
        if path == "/api/history":
            return self._json(history((qs.get("range") or ["1D"])[0].upper()))
        if path == "/api/bars":
            code, payload = bars((qs.get("symbol") or [""])[0].upper())
            return self._json(payload, code)
        if path == "/api/assets":
            return self._json(assets())
        if path == "/api/events":
            import time
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(("event: state\ndata: %s\n\n" % json.dumps(state())).encode())
            self.wfile.flush()
            time.sleep(40)
            return
        if path == "/api/health":
            return self._json({"ok": True})
        self._send(404, "text/plain", b"not found\n", "no-store")

    def _json(self, payload, code=200):
        self._send(code, "application/json", json.dumps(payload).encode(), "no-store")

    def _send(self, code, ctype, body, cache, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("synthetic UI-4 preview http://%s:%d/  legacy at /legacy" % (HOST, PORT))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
