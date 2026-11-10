#!/usr/bin/env python3
"""Trading Desk: a read-only live control room for an Alpaca equity desk.

One asyncio process (aiohttp):
  * Alpaca trade_updates websocket   (wss://api.alpaca.markets/stream)
  * Alpaca market-data websocket     (wss://stream.data.alpaca.markets/v2/<feed>)
  * REST resync (GET only) of account, positions, open orders, today's fills,
    and 1D/5Min portfolio history, on every (re)connect and once a minute
  * desk status feed (JSON file or HA sensor) for bot tiles, pipeline, caps,
    rotation proposals
  * pushes one JSON snapshot to browsers over Server-Sent Events

STRICTLY READ-ONLY: the only outbound HTTP method is GET (enforced at runtime
by a request hook and at build time by selfcheck.py). No order endpoints.
"""
import asyncio
import datetime as dt
import json
import logging
import math
import os
import pathlib
import random
import re
import sys
import time
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

import selfcheck

APP_DIR = pathlib.Path(__file__).resolve().parent
STATIC = APP_DIR / "static"
EXAMPLE_STATUS = APP_DIR / "example_status.json"
ET = ZoneInfo("America/New_York")
VERSION = "0.3.3"
BOTS = ["Desk Lead", "Strategist", "Risk Manager", "Research Analyst",
        "Execution Trader", "Trade Reviewer", "Trading Floor"]
DEFAULT_STATUS_PATHS = [   # spec order: HA config custom_templates first, then /share
    "/homeassistant/custom_templates/trading_desk/status.json",
    "/config/custom_templates/trading_desk/status.json",
    "/share/trading_desk/status.json",
]
ASSETS_PATHS = [            # fed Schwab-style snapshot (manual/fed JSON only, no brokerage login)
    "/share/trading_desk/assets.json",                     # {"assets": {...}} or the bare assets object
    "/homeassistant/pyscript/.life_hq_feed.json",          # Life HQ private feed: its "assets" key (same push feeds both)
]
ASSETS_STALE_H = 6
ASSETS_MAX_BYTES = 400_000
STATUS_STALE_S = 15 * 60   # feed older than this is flagged stale in the UI

# --------------------------------------------------------------------------
# options + secret redaction
# --------------------------------------------------------------------------

def load_options():
    path = os.environ.get("TD_OPTIONS", "/data/options.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


OPTS = load_options()
KEY_ID = (OPTS.get("api_key_id") or "").strip()
SECRET = (OPTS.get("api_secret_key") or "").strip()
MOCK = bool(OPTS.get("mock")) or os.environ.get("TD_MOCK") == "1"
ENV = OPTS.get("trading_env", "live")
FEED = OPTS.get("data_feed", "iex")
DATA_STREAM_ON = bool(OPTS.get("data_stream", True))
PORT = int(os.environ.get("TD_PORT", "8099"))
def _load_supervisor_token():
    """s6-overlay (base image /init) starts CMD with a clean env; the Supervisor token is
    kept in s6's container_environment. Copy it into os.environ so IN_HA and the
    Supervisor API calls (Trezor helper, HA price sensors, status_entity) work."""
    if not os.environ.get("SUPERVISOR_TOKEN"):
        try:
            tok = pathlib.Path("/run/s6/container_environment/SUPERVISOR_TOKEN").read_text().strip()
            if tok:
                os.environ["SUPERVISOR_TOKEN"] = tok
        except OSError:
            pass


_load_supervisor_token()
IN_HA = bool(os.environ.get("SUPERVISOR_TOKEN"))
GH_REPO = (OPTS.get("feed_github_repo") or "").strip().strip("/")
GH_PATH = (OPTS.get("feed_github_path") or "status.json").strip().lstrip("/")
GH_TOKEN = (OPTS.get("feed_github_token") or "").strip()
GH_POLL_S = max(60, int(OPTS.get("feed_github_poll_s") or 60))
# Trezor (cold wallet) BTC: quantity from an editable HA helper, valued at a public spot price.
TREZOR_ENTITY = (OPTS.get("trezor_btc_entity") or "input_number.trezor_btc").strip()
BTC_SPOT_URL = "https://api.coinbase.com/v2/prices/BTC-USD/spot"          # public, no key
BTC_STATS_URL = "https://api.exchange.coinbase.com/products/BTC-USD/stats"  # public, no key (24 h open)
CRYPTO_POLL_S = 60
# Main-tab range buttons -> Alpaca portfolio/history params. Alpaca is the only source with history.
RANGES = {"1D": {"period": "1D", "timeframe": "5Min", "intraday_reporting": "market_hours"},
          "1W": {"period": "1W", "timeframe": "1H"},
          "1M": {"period": "1M", "timeframe": "1D"},
          "1A": {"period": "1A", "timeframe": "1D"},
          "5A": {"period": "5A", "timeframe": "1D"}}

TRADE_REST = "https://api.alpaca.markets" if ENV == "live" else "https://paper-api.alpaca.markets"
TRADE_WS = "wss://api.alpaca.markets/stream" if ENV == "live" else "wss://paper-api.alpaca.markets/stream"
DATA_REST = "https://data.alpaca.markets"
DATA_WS = f"wss://stream.data.alpaca.markets/v2/{FEED}"


def mask(s) -> str:
    """Mask a secret to its last 4 characters only (never echo more)."""
    s = str(s or "")
    return ("****" + s[-4:]) if len(s) > 8 else "****"


def _secrets():
    # longest first so a secret that contains another is masked whole
    vals = [KEY_ID, SECRET, GH_TOKEN, os.environ.get("SUPERVISOR_TOKEN", "")]
    return sorted({v for v in vals if v and len(v) >= 4}, key=len, reverse=True)


def redact(text) -> str:
    text = str(text)
    for s in _secrets():
        text = text.replace(s, mask(s))
    return text


class Redact(logging.Filter):
    """Mask the API key id, secret, feed token and Supervisor token (last 4 only) in every log record."""

    def filter(self, record):
        record.msg, record.args = redact(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


logging.basicConfig(level=getattr(logging, str(OPTS.get("log_level", "info")).upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
for h in logging.getLogger().handlers:
    h.addFilter(Redact())
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("trading_desk")


# --------------------------------------------------------------------------
# GET-only HTTP session
# --------------------------------------------------------------------------

async def _only_get(session, ctx, params):
    if params.method.upper() != "GET":
        raise RuntimeError("Trading Desk is read-only: blocked non-GET request")


def make_session():
    tc = aiohttp.TraceConfig()
    tc.on_request_start.append(_only_get)
    return aiohttp.ClientSession(trace_configs=[tc], timeout=aiohttp.ClientTimeout(total=20))


def num_s(x):
    v = fnum(x)
    return "?" if v is None else (f"{v:g}")


def now_ms():
    return int(time.time() * 1000)


def iso_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def fnum(x, default=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


class AuthError(RuntimeError):
    """Bad or missing keys: retry slowly instead of hammering Alpaca."""


class Backoff:
    """Exponential backoff with full jitter."""

    def __init__(self, base=1.0, cap=60.0):
        self.base, self.cap, self.n = base, cap, 0

    def next(self, floor=0.0):
        d = min(self.cap, self.base * (2 ** self.n))
        self.n = min(self.n + 1, 10)
        return max(floor, random.uniform(d / 2, d))

    def reset(self):
        self.n = 0


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class Desk:
    def __init__(self):
        self.account = {}
        self.positions = []
        self.orders = []
        self.fills = []
        self.history = {"t": [], "equity": []}
        # Alpaca period P&L from portfolio/history (week/month/year); day from account.
        self.periods = {"day": None, "week": None, "month": None, "year": None, "as_of_ms": None}
        self.live_prices = {}          # symbol -> (price, ts_ms)
        # Alpaca portfolio/history series per range for the Main tab chart.
        self.series = {}               # range -> {"t": [...], "equity": [...], "pl": [...], ...}
        self.crypto = {"btc_qty": None, "qty_source": TREZOR_ENTITY, "qty_error": None,
                       "btc_price": None, "btc_open_24h": None, "price_source": None,
                       "price_as_of_ms": None, "price_error": None}
        self.trade_events = []         # recent trade_updates
        self.status = {}
        self.status_source = None
        self.status_error = None
        self.health = {
            "trade_stream": {"state": "idle", "last_msg_ms": None, "connected_since_ms": None,
                             "reconnects": 0, "error": None},
            "data_stream": {"state": "idle" if DATA_STREAM_ON else "disabled", "last_msg_ms": None,
                            "connected_since_ms": None, "reconnects": 0, "error": None,
                            "feed": FEED, "symbols": []},
            "rest": {"last_resync_ms": None, "last_ok_ms": None, "error": None, "resyncs": 0},
            "status_feed": {"last_read_ms": None, "source": None, "error": None, "example": False},
        }
        self.clients = set()
        self.dirty = asyncio.Event()
        self.resync_now = asyncio.Event()
        self.symbols_changed = asyncio.Event()
        self.session = None

    # ------------------------------------------------------------- derived
    def held_symbols(self):
        return sorted({p.get("symbol") for p in self.positions if p.get("symbol")})

    def snapshot(self):
        acct = self.account or {}
        equity_rest = fnum(acct.get("equity"))
        last_equity = fnum(acct.get("last_equity"))
        rows, live_adj = [], 0.0
        for p in self.positions:
            sym = p.get("symbol")
            qty = fnum(p.get("qty"), 0.0)
            entry = fnum(p.get("avg_entry_price"), 0.0)
            rest_px = fnum(p.get("current_price"), entry)
            lp = self.live_prices.get(sym)
            px = lp[0] if lp else rest_px
            live_adj += qty * (px - rest_px)
            mv = qty * px
            upl = qty * (px - entry)
            cost = abs(qty * entry)
            prev_close = fnum(p.get("lastday_price"))
            rows.append({
                "symbol": sym, "qty": qty, "side": p.get("side"), "avg_entry": entry,
                "price": px, "price_live": bool(lp), "price_ts_ms": lp[1] if lp else None,
                "market_value": mv, "unrealized_pl": upl,
                "unrealized_plpc": (upl / cost * 100) if cost else None,
                "day_change_pct": ((px / prev_close - 1) * 100) if prev_close else None,
            })
        equity = (equity_rest + live_adj) if equity_rest is not None else None
        day_pl = (equity - last_equity) if (equity is not None and last_equity) else None
        day_plpc = (day_pl / last_equity * 100) if (day_pl is not None and last_equity) else None
        risk = self.risk(rows, equity)
        rows.sort(key=lambda r: -abs(r["market_value"]))
        hist_t, hist_e = list(self.history.get("t", [])), list(self.history.get("equity", []))
        if equity is not None:
            hist_t.append(int(time.time()))
            hist_e.append(equity)
        st = self.status or {}
        return {
            "v": VERSION, "ts_ms": now_ms(), "mock": MOCK, "env": ENV,
            "keys_configured": bool(KEY_ID and SECRET) or MOCK,
            "health": self.health,
            "account": {"equity": equity, "last_equity": last_equity, "day_pl": day_pl,
                        "day_plpc": day_plpc, "cash": fnum(acct.get("cash")),
                        "buying_power": fnum(acct.get("buying_power")),
                        "long_market_value": fnum(acct.get("long_market_value")),
                        "status": acct.get("status"),
                        "trading_blocked": acct.get("trading_blocked")},
            "positions": rows,
            "orders": [{"symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("type"),
                        "qty": fnum(o.get("qty")), "stop_price": fnum(o.get("stop_price")),
                        "limit_price": fnum(o.get("limit_price")), "status": o.get("status"),
                        "submitted_at": o.get("submitted_at")} for o in self.flat_orders()],
            "fills": self.fills[:100],
            "curve": {"t": hist_t, "equity": hist_e, "base": last_equity},
            "risk": risk,
            "trade_events": self.trade_events[:30],
            "desk": {"bots": self.bot_tiles(risk), "pipeline": self.pipeline(),
                     "proposals": st.get("proposals") or [], "updated": st.get("updated"),
                     "example": bool(st.get("example")), "source": self.status_source,
                     "feed": self.feed_state(),
                     "thresholds": st.get("thresholds") or [], "error": self.status_error,
                     "holdings": st.get("holdings"), "portfolio": st.get("portfolio")},
            "totals": self.totals(equity, day_pl, st.get("holdings") or {}),
            "crypto": self.crypto_view(),
            "ha_prices": getattr(self, "ha_prices", {}),
            "ranges": self.ranges_view(),
            "gauges": self.gauges(risk, day_plpc),
        }


    def totals(self, alpaca_equity, alpaca_day_pl, holdings):
        """Combined Schwab+Alpaca assets and period gains for the top strip."""
        h = holdings or {}
        schwab = fnum(h.get("schwab_total"))
        alpaca = alpaca_equity if alpaca_equity is not None else fnum(h.get("alpaca_equity"))
        combined = None
        if schwab is not None and alpaca is not None:
            combined = schwab + alpaca
        elif schwab is not None:
            combined = schwab
        elif alpaca is not None:
            combined = alpaca
        schwab_day = fnum(h.get("schwab_day_pl"))
        schwab_day_pc = fnum(h.get("schwab_day_plpc"))
        schwab_unrl = fnum(h.get("schwab_unrealized_pl"))
        # Alpaca unrealized from open positions
        alpaca_unrl = 0.0
        have_unrl = False
        for p in self.positions or []:
            u = fnum(p.get("unrealized_pl"))
            if u is not None:
                alpaca_unrl += u
                have_unrl = True
        if not have_unrl:
            alpaca_unrl = None
        periods = self.periods or {}
        def _pl(key):
            p = periods.get(key) or {}
            return p.get("pl"), p.get("plpc")
        a_day, a_day_pc = _pl("day")
        if a_day is None and alpaca_day_pl is not None:
            a_day = alpaca_day_pl
        # Combined day = Schwab day + Alpaca day when both present
        c_day = None
        if schwab_day is not None and a_day is not None:
            c_day = schwab_day + a_day
        elif schwab_day is not None:
            c_day = schwab_day
        elif a_day is not None:
            c_day = a_day
        c_unrl = None
        if schwab_unrl is not None and alpaca_unrl is not None:
            c_unrl = schwab_unrl + alpaca_unrl
        elif schwab_unrl is not None:
            c_unrl = schwab_unrl
        elif alpaca_unrl is not None:
            c_unrl = alpaca_unrl
        a_week, a_week_pc = _pl("week")
        a_month, a_month_pc = _pl("month")
        a_year, a_year_pc = _pl("year")
        return {
            "combined_total": combined,
            "schwab_total": schwab,
            "alpaca_equity": alpaca,
            "day": {"combined_pl": c_day, "alpaca_pl": a_day, "alpaca_plpc": a_day_pc,
                    "schwab_pl": schwab_day, "schwab_plpc": schwab_day_pc},
            "week": {"alpaca_pl": a_week, "alpaca_plpc": a_week_pc, "scope": "alpaca"},
            "month": {"alpaca_pl": a_month, "alpaca_plpc": a_month_pc, "scope": "alpaca"},
            "year": {"alpaca_pl": a_year, "alpaca_plpc": a_year_pc, "scope": "alpaca"},
            "unrealized": {"combined_pl": c_unrl, "alpaca_pl": alpaca_unrl, "schwab_pl": schwab_unrl},
            "schwab_as_of": h.get("as_of"),
            "schwab_price_as_of": h.get("price_as_of"),
            "schwab_source": h.get("source"),
            "periods_as_of_ms": periods.get("as_of_ms"),
        }

    def crypto_view(self):
        c = dict(self.crypto)
        q, px, op = c.get("btc_qty"), c.get("btc_price"), c.get("btc_open_24h")
        c["value"] = (q * px) if (q is not None and px is not None) else None
        c["day_pl"] = (q * (px - op)) if (q is not None and px is not None and op) else None
        c["day_plpc"] = ((px / op - 1) * 100) if (px is not None and op) else None
        return c

    def ranges_view(self):
        """Per-range gain for the Main tab. Alpaca only (net of deposits); no invented data."""
        out = {}
        for key, ser in self.series.items():
            out[key] = {k: ser.get(k) for k in ("pl", "plpc", "covers_from", "points", "error", "scope", "basis")}
        return out

    # ------------------------------------------------------------- desk feed
    def feed_state(self):
        """'live' | 'stale' | 'missing' | 'example' for the desk status feed."""
        st = self.status or {}
        if st.get("example"):
            return "example"
        if not st:
            return "missing"
        try:
            upd = dt.datetime.fromisoformat(str(st.get("updated")).replace("Z", "+00:00"))
            if (dt.datetime.now(dt.timezone.utc) - upd).total_seconds() > STATUS_STALE_S:
                return "stale"
        except (TypeError, ValueError):
            return "stale"
        return "live"

    def pipeline(self):
        """Feed pipeline events plus today's Alpaca fills (stage 'fill', always real)."""
        evs = [e for e in ((self.status or {}).get("pipeline") or []) if isinstance(e, dict)][-60:]
        seen = {(e.get("symbol"), e.get("stage"), str(e.get("ts"))[:16]) for e in evs}
        for f in self.fills[:20]:
            key = (f.get("symbol"), "fill", str(f.get("time"))[:16])
            if key in seen:
                continue
            seen.add(key)
            evs.append({"trade_id": f.get("order_id") or f"{f.get('symbol')}-{f.get('time')}",
                        "symbol": f.get("symbol"), "stage": "fill", "ts": f.get("time"),
                        "source": "alpaca", "note": f"{f.get('side')} {f.get('qty')} @ {f.get('price')}"})
        return evs

    def gauges(self, risk, day_plpc):
        """Numeric thresholds with breach colouring: computed risk + numeric feed thresholds."""
        caps, out = risk["caps"], []
        out.append({"name": "Open risk", "value": risk["open_risk_pct"],
                    "limit": caps["temp_open_risk_pct"]["value"], "unit": "%",
                    "source": caps["temp_open_risk_pct"]["source"]})
        lg = risk["largest"] or {}
        out.append({"name": "Largest name" + (f" ({lg.get('symbol')})" if lg else ""),
                    "value": lg.get("weight_pct"), "limit": caps["single_name_cap_pct"]["value"],
                    "unit": "%", "source": caps["single_name_cap_pct"]["source"]})
        out.append({"name": "Unprotected positions (no resting stop)", "value": len(risk["unprotected"]),
                    "limit": 0, "unit": "", "source": "Alpaca open orders", "zero_ok": True})
        for t in (self.status or {}).get("thresholds") or []:
            if not isinstance(t, dict):
                continue
            v, lim = fnum(t.get("current")), fnum(t.get("limit"))
            if v is None or lim is None:
                continue
            out.append({"name": str(t.get("name") or "threshold"), "value": v, "limit": lim,
                        "unit": str(t.get("unit") or ""), "source": "desk status feed",
                        "invert": bool(t.get("lower_is_breach"))})
        if day_plpc is not None:
            out.append({"name": "Day P&L", "value": day_plpc, "limit": None, "unit": "%",
                        "source": "Alpaca account", "info": True})
        return out

    def flat_orders(self):
        out = []
        for o in self.orders:
            out.append(o)
            for leg in o.get("legs") or []:
                out.append(leg)
        return [o for o in out if o.get("status") not in ("filled", "canceled", "expired", "rejected")]

    def risk(self, rows, equity):
        """Open risk = sum over long positions of qty * (entry - stop), floored at 0.

        Stops come from resting stop / stop_limit / trailing_stop sell orders.
        Shares with no resting stop are reported as unprotected (not guessed).
        """
        stops = {}
        for o in self.flat_orders():
            if o.get("type") in ("stop", "stop_limit", "trailing_stop"):
                sp = fnum(o.get("stop_price"))
                if sp is None:
                    continue
                stops.setdefault((o.get("symbol"), o.get("side")), []).append((sp, fnum(o.get("qty"), 0.0)))
        open_risk, unprotected, per = 0.0, [], []
        for r in rows:
            qty = r["qty"]
            exit_side = "sell" if qty > 0 else "buy"
            covered, rsk = 0.0, 0.0
            for sp, q in stops.get((r["symbol"], exit_side), []):
                q = min(abs(q), abs(qty) - covered)
                if q <= 0:
                    break
                covered += q
                rsk += q * max(0.0, (r["avg_entry"] - sp) if qty > 0 else (sp - r["avg_entry"]))
            open_risk += rsk
            unc = abs(qty) - covered
            if unc > 1e-9:
                unprotected.append({"symbol": r["symbol"], "qty": unc})
            per.append({"symbol": r["symbol"], "risk_usd": rsk,
                        "weight_pct": (abs(r["market_value"]) / equity * 100) if equity else None})
        largest = max(per, key=lambda x: x["weight_pct"] or 0, default=None)
        caps = self.caps()
        desk_risk = (self.status or {}).get("risk") or {}
        return {
            "open_risk_usd": open_risk,
            "open_risk_pct": (open_risk / equity * 100) if equity else None,
            "desk_open_risk_pct": fnum(desk_risk.get("open_risk_pct")),
            "unprotected": unprotected,
            "largest": largest,
            "per_symbol": per,
            "caps": caps,
        }

    def caps(self):
        st_caps = (self.status or {}).get("caps") or {}
        example = bool((self.status or {}).get("example"))
        out = {}
        for key, opt in (("temp_open_risk_pct", "temp_open_risk_cap_pct"),
                         ("single_name_cap_pct", "single_name_cap_pct")):
            if fnum(OPTS.get(opt)) is not None:
                out[key] = {"value": fnum(OPTS.get(opt)), "source": "add-on option"}
            elif fnum(st_caps.get(key)) is not None:
                out[key] = {"value": fnum(st_caps.get(key)),
                            "source": "EXAMPLE status file" if example else "desk status feed"}
            else:
                out[key] = {"value": None, "source": "unknown"}
        return out

    def derived_bots(self, risk):
        """Tiles the add-on can fill from Alpaca alone (used when the feed lacks them)."""
        out = {}
        last_fill = self.fills[0] if self.fills else None
        ev = self.trade_events[0] if self.trade_events else None
        open_n = len(self.flat_orders())
        if ev:
            act = f"{ev.get('event')}: {ev.get('side') or ''} {ev.get('symbol') or ''}".strip()
            ts = ev.get("ts")
        elif last_fill:
            act = f"filled {last_fill.get('side')} {num_s(last_fill.get('qty'))} {last_fill.get('symbol')} @ {last_fill.get('price')}"
            ts = last_fill.get("time")
        else:
            act, ts = "no fills today", None
        out["Execution Trader"] = {"status": "working" if ev and ev.get("event") in ("new", "partial_fill", "accepted", "pending_new") else "idle",
                                   "last_action": f"{act} · {open_n} open orders", "ts": ts,
                                   "detail": "from Alpaca", "derived": True}
        cap = risk["caps"]["temp_open_risk_pct"]["value"]
        orp = risk["open_risk_pct"]
        st = "idle"
        if risk["unprotected"]:
            st = "alert"
        if cap is not None and orp is not None and orp >= cap:
            st = "blocked"
        msg = (f"open risk {orp:.2f}%" if orp is not None else "open risk n/a") + \
              (f" of {cap:g}% cap" if cap is not None else "") + \
              (f" · {len(risk['unprotected'])} without stop" if risk["unprotected"] else "")
        out["Risk Manager"] = {"status": st, "last_action": msg, "ts": iso_now(),
                               "detail": "computed by add-on", "derived": True}
        return out

    def bot_tiles(self, risk=None):
        given = {b.get("name"): b for b in (self.status or {}).get("bots") or [] if isinstance(b, dict)}
        if risk is not None:
            for name, b in self.derived_bots(risk).items():
                given.setdefault(name, b)
        tiles = []
        for name in BOTS:
            b = given.get(name, {"status": "no feed", "last_action": ""})
            tiles.append({"name": name, "status": b.get("status") or "unknown",
                          "last_action": b.get("last_action") or "", "ts": b.get("ts"),
                          "detail": b.get("detail") or "", "derived": bool(b.get("derived"))})
        for name, b in given.items():
            if name not in BOTS:
                tiles.append({"name": name, "status": b.get("status") or "unknown",
                              "last_action": b.get("last_action") or "", "ts": b.get("ts"),
                              "detail": b.get("detail") or ""})
        return tiles

    def mark(self):
        self.dirty.set()


DESK = None

# --------------------------------------------------------------------------
# Alpaca REST (GET only)
# --------------------------------------------------------------------------

def auth_headers():
    return {"APCA-API-KEY-ID": KEY_ID, "APCA-API-SECRET-KEY": SECRET}


async def rest_get(base, path, params=None):
    async with DESK.session.get(base + path, params=params, headers=auth_headers()) as r:
        if r.status != 200:
            body = (await r.text())[:200]
            raise RuntimeError(f"GET {path} -> HTTP {r.status}: {redact(body)}")
        return await r.json()


def et_midnight_utc_iso():
    d = dt.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")



def _period_from_hist(hist):
    """Gain over a portfolio/history window, NET of deposits/withdrawals.

    Alpaca's profit_loss series already excludes cash flows, so its last point is the
    period gain. Raw equity end-minus-start is only a fallback (it counts deposits as
    gain, which made 'Year' read +198% on an account opened mid-September).
    """
    if not hist:
        return None
    pls = [p for p in (hist.get("profit_loss") or []) if p is not None]
    pcs = [p for p in (hist.get("profit_loss_pct") or []) if p is not None]
    if pls:
        pl = pls[-1]
        pct = (pcs[-1] * 100.0) if pcs else None
        base = hist.get("base_value")
        return {"pl": pl, "plpc": pct, "base": base, "end": None, "basis": "profit_loss (net of deposits)"}
    eq = [e for e in (hist.get("equity") or []) if e is not None and e != 0]
    if len(eq) < 2:
        # fall back to last profit_loss point when equity series is short
        pls = [p for p in (hist.get("profit_loss") or []) if p is not None]
        if not pls:
            return None
        pl = pls[-1]
        base = hist.get("base_value")
        pct = (pl / base * 100.0) if base else None
        return {"pl": pl, "plpc": pct, "base": base, "end": (base + pl) if base is not None else None}
    start, end = eq[0], eq[-1]
    pl = end - start
    pct = (pl / start * 100.0) if start else None
    return {"pl": pl, "plpc": pct, "base": start, "end": end, "basis": "equity change (includes deposits)"}


def _series_from_hist(key, hist):
    """Chart series + coverage for one range. Leading zero-equity points (before the
    account was funded) are dropped so 'covers_from' shows the real start."""
    ts = hist.get("timestamp") or []
    eq = hist.get("equity") or []
    pl = hist.get("profit_loss") or []
    rows = [(t, e, (pl[i] if i < len(pl) else None)) for i, (t, e) in enumerate(zip(ts, eq)) if e]
    per = _period_from_hist(hist) or {}
    return {"t": [r[0] for r in rows], "equity": [r[1] for r in rows], "profit_loss": [r[2] for r in rows],
            "pl": per.get("pl"), "plpc": per.get("plpc"), "basis": per.get("basis"),
            "covers_from": rows[0][0] if rows else None, "points": len(rows),
            "scope": "alpaca", "error": None if rows else "no history returned"}


async def resync_ranges():
    """Fetch every Main-tab range separately so one rejected period (e.g. 5A) never
    breaks the others or the main resync."""
    for key, params in RANGES.items():
        try:
            DESK.series[key] = _series_from_hist(key, await rest_get(TRADE_REST, "/v2/account/portfolio/history", params))
        except Exception as e:  # noqa: BLE001
            prev = DESK.series.get(key) or {}
            DESK.series[key] = {**prev, "error": redact(e)[:200], "scope": "alpaca"}


async def resync(reason):
    h = DESK.health["rest"]
    h["last_resync_ms"] = now_ms()
    h["resyncs"] += 1
    if MOCK:
        mock_resync()
        h["last_ok_ms"], h["error"] = now_ms(), None
        DESK.mark()
        return
    try:
        acct, pos, orders, fills, hist, hist_w, hist_m, hist_y = await asyncio.gather(
            rest_get(TRADE_REST, "/v2/account"),
            rest_get(TRADE_REST, "/v2/positions"),
            rest_get(TRADE_REST, "/v2/orders", {"status": "open", "nested": "true", "limit": "500"}),
            rest_get(TRADE_REST, "/v2/account/activities/FILL",
                     {"after": et_midnight_utc_iso(), "direction": "desc", "page_size": "100"}),
            rest_get(TRADE_REST, "/v2/account/portfolio/history",
                     {"period": "1D", "timeframe": "5Min", "intraday_reporting": "market_hours"}),
            rest_get(TRADE_REST, "/v2/account/portfolio/history",
                     {"period": "1W", "timeframe": "1D"}),
            rest_get(TRADE_REST, "/v2/account/portfolio/history",
                     {"period": "1M", "timeframe": "1D"}),
            rest_get(TRADE_REST, "/v2/account/portfolio/history",
                     {"period": "1A", "timeframe": "1D"}),
        )
        old = DESK.held_symbols()
        DESK.account, DESK.positions, DESK.orders = acct, pos, orders
        DESK.fills = [{"symbol": f.get("symbol"), "side": f.get("side"), "qty": fnum(f.get("qty")),
                       "price": fnum(f.get("price")), "time": f.get("transaction_time"),
                       "type": f.get("type"), "order_id": f.get("order_id")} for f in fills]
        ts = hist.get("timestamp") or []
        eq = hist.get("equity") or []
        pairs = [(t, e) for t, e in zip(ts, eq) if e is not None]
        DESK.history = {"t": [p[0] for p in pairs], "equity": [p[1] for p in pairs]}
        # Day from account last_equity; W/M/Y from history equity endpoints (Alpaca only).
        day_pl = None
        try:
            eq_now = fnum(acct.get("equity"))
            last_eq = fnum(acct.get("last_equity"))
            if eq_now is not None and last_eq:
                day_pl = {"pl": eq_now - last_eq,
                          "plpc": (eq_now - last_eq) / last_eq * 100.0,
                          "base": last_eq, "end": eq_now}
        except Exception:  # noqa: BLE001
            day_pl = None
        DESK.periods = {
            "day": day_pl or _period_from_hist(hist),
            "week": _period_from_hist(hist_w),
            "month": _period_from_hist(hist_m),
            "year": _period_from_hist(hist_y),
            "as_of_ms": now_ms(),
            "scope": "alpaca",
        }
        # drop live prices for symbols no longer held
        held = set(DESK.held_symbols())
        DESK.live_prices = {k: v for k, v in DESK.live_prices.items() if k in held}
        if DESK.held_symbols() != old:
            DESK.symbols_changed.set()
        await resync_ranges()
        h["last_ok_ms"], h["error"] = now_ms(), None
        log.info("resync ok (%s): %d positions, %d open orders, %d fills today",
                 reason, len(pos), len(orders), len(fills))
    except Exception as e:  # noqa: BLE001
        h["error"] = redact(e)
        log.warning("resync failed (%s): %s", reason, redact(e))
    DESK.mark()


async def resync_loop():
    while True:
        try:
            await asyncio.wait_for(DESK.resync_now.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
        DESK.resync_now.clear()
        await asyncio.sleep(0.5)  # debounce bursts of fills
        await resync("periodic" if not DESK.resync_now.is_set() else "event")


async def price_poll_loop():
    """Fallback when the data websocket is off or down: GET latest trades every 15 s."""
    while True:
        await asyncio.sleep(15)
        if MOCK or not KEY_ID or DESK.health["data_stream"]["state"] == "streaming":
            continue
        syms = DESK.held_symbols()
        if not syms:
            continue
        try:
            data = await rest_get(DATA_REST, "/v2/stocks/trades/latest",
                                  {"symbols": ",".join(syms), "feed": FEED})
            for sym, tr in (data.get("trades") or {}).items():
                p = fnum(tr.get("p"))
                if p:
                    DESK.live_prices[sym] = (p, now_ms())
            DESK.mark()
        except Exception as e:  # noqa: BLE001
            log.debug("price poll failed: %s", redact(e))


# --------------------------------------------------------------------------
# Alpaca websockets
# --------------------------------------------------------------------------

def decode(msg):
    if msg.type == aiohttp.WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == aiohttp.WSMsgType.BINARY:
        return json.loads(msg.data.decode("utf-8"))
    return None


async def trade_stream():
    h = DESK.health["trade_stream"]
    bo = Backoff(1, 60)
    while True:
        h["state"] = "connecting"
        DESK.mark()
        try:
            async with DESK.session.ws_connect(TRADE_WS, heartbeat=20, receive_timeout=90) as ws:
                await ws.send_json({"action": "auth", "key": KEY_ID, "secret": SECRET})
                await ws.send_json({"action": "listen", "data": {"streams": ["trade_updates"]}})
                async for msg in ws:
                    if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    data = decode(msg)
                    if not data:
                        continue
                    h["last_msg_ms"] = now_ms()
                    stream = data.get("stream")
                    payload = data.get("data") or {}
                    if stream == "authorization":
                        if payload.get("status") != "authorized":
                            raise AuthError(f"trade stream auth: {payload.get('status')}")
                    elif stream == "listening":
                        h["state"], h["error"] = "streaming", None
                        h["connected_since_ms"] = now_ms()
                        bo.reset()
                        DESK.resync_now.set()   # REST resync on every (re)connect
                    elif stream == "trade_updates":
                        o = payload.get("order") or {}
                        DESK.trade_events.insert(0, {
                            "event": payload.get("event"), "symbol": o.get("symbol"),
                            "side": o.get("side"), "type": o.get("type"),
                            "qty": fnum(payload.get("qty") or o.get("qty")),
                            "price": fnum(payload.get("price")),
                            "ts": payload.get("timestamp") or iso_now()})
                        del DESK.trade_events[50:]
                        DESK.resync_now.set()
                    DESK.mark()
            raise RuntimeError("trade stream closed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            h["state"], h["error"] = ("auth failed" if isinstance(e, AuthError) else "reconnecting"), redact(e)
            h["reconnects"] += 1
            h["connected_since_ms"] = None
            DESK.mark()
            delay = bo.next(60.0 if isinstance(e, AuthError) else 0.0)
            log.warning("trade stream: %s; retry in %.1fs", redact(e), delay)
            await asyncio.sleep(delay)


async def data_stream():
    h = DESK.health["data_stream"]
    bo = Backoff(1, 120)
    while True:
        h["state"] = "connecting"
        DESK.mark()
        floor = 0.0
        try:
            async with DESK.session.ws_connect(DATA_WS, heartbeat=20, receive_timeout=90) as ws:
                await ws.send_json({"action": "auth", "key": KEY_ID, "secret": SECRET})
                subscribed = set()
                authed = False

                async def sync_subs():
                    want = set(DESK.held_symbols())
                    add, drop = sorted(want - subscribed), sorted(subscribed - want)
                    if drop:
                        await ws.send_json({"action": "unsubscribe", "trades": drop})
                    if add:
                        await ws.send_json({"action": "subscribe", "trades": add})
                    subscribed.clear()
                    subscribed.update(want)
                    h["symbols"] = sorted(want)

                async def watch_symbols():
                    while True:
                        await DESK.symbols_changed.wait()
                        DESK.symbols_changed.clear()
                        if authed:
                            await sync_subs()

                watcher = asyncio.create_task(watch_symbols())
                try:
                    async for msg in ws:
                        if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                        items = decode(msg)
                        if items is None:
                            continue
                        h["last_msg_ms"] = now_ms()
                        for it in items if isinstance(items, list) else [items]:
                            t = it.get("T")
                            if t == "success" and it.get("msg") == "authenticated":
                                authed = True
                                h["state"], h["error"] = "streaming", None
                                h["connected_since_ms"] = now_ms()
                                bo.reset()
                                await sync_subs()
                            elif t == "error":
                                code = it.get("code")
                                if code == 406:   # connection limit (another client holds it)
                                    floor = 300.0
                                if code in (401, 402, 403, 409):  # auth / plan problems
                                    floor = 120.0
                                raise RuntimeError(f"data stream error {code}: {it.get('msg')}")
                            elif t == "t":
                                p = fnum(it.get("p"))
                                if p:
                                    DESK.live_prices[it.get("S")] = (p, now_ms())
                        DESK.mark()
                finally:
                    watcher.cancel()
            raise RuntimeError("data stream closed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            h["state"], h["error"] = "reconnecting", redact(e)
            h["reconnects"] += 1
            h["connected_since_ms"] = None
            DESK.mark()
            delay = bo.next(floor)
            log.warning("data stream: %s; retry in %.1fs (REST price poll covers the gap)",
                        redact(e), delay)
            await asyncio.sleep(delay)


# --------------------------------------------------------------------------
# Desk status feed (file or HA sensor)
# --------------------------------------------------------------------------

GH_CACHE = {"etag": None, "data": None, "next": 0.0, "err": None, "ok_ms": None}


async def github_feed():
    """Read-only GET of the desk feed from a private GitHub repo (contents API, raw).

    ETag-cached (a 304 does not count against the rate limit), polled at most
    every GH_POLL_S (>= 60 s). Returns the last good document or None.
    """
    if not GH_REPO:
        return None
    now = time.monotonic()
    if now >= GH_CACHE["next"]:
        GH_CACHE["next"] = now + GH_POLL_S
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_PATH}"
        hdr = {"Accept": "application/vnd.github.raw+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "trading-desk-addon"}
        if GH_TOKEN:
            hdr["Authorization"] = f"Bearer {GH_TOKEN}"
        if GH_CACHE["etag"]:
            hdr["If-None-Match"] = GH_CACHE["etag"]
        try:
            async with DESK.session.get(url, headers=hdr, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    GH_CACHE["data"] = json.loads(await r.text())
                    GH_CACHE["etag"] = r.headers.get("ETag")
                    GH_CACHE["err"], GH_CACHE["ok_ms"] = None, now_ms()
                elif r.status == 304:
                    GH_CACHE["err"], GH_CACHE["ok_ms"] = None, now_ms()
                else:
                    GH_CACHE["err"] = f"github {GH_REPO}/{GH_PATH}: HTTP {r.status}"
                    log.warning("desk feed: %s", GH_CACHE["err"])
        except Exception as e:  # noqa: BLE001
            GH_CACHE["err"] = f"github: {redact(e)}"
            log.warning("desk feed: %s", GH_CACHE["err"])
    return GH_CACHE["data"]


async def read_status():
    h = DESK.health["status_feed"]
    entity = (OPTS.get("status_entity") or "").strip()
    paths = [OPTS["status_file"]] if (OPTS.get("status_file") or "").strip() else DEFAULT_STATUS_PATHS
    paths = paths + ([os.environ["TD_STATUS_FILE"]] if os.environ.get("TD_STATUS_FILE") else [])
    data, source, err = None, None, None
    if entity and IN_HA:
        try:
            url = f"http://supervisor/core/api/states/{entity}"
            hdr = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}
            async with DESK.session.get(url, headers=hdr) as r:
                if r.status == 200:
                    st = await r.json()
                    attrs = st.get("attributes") or {}
                    data = attrs.get("status") if isinstance(attrs.get("status"), dict) else attrs
                    source = f"entity {entity}"
                else:
                    err = f"{entity}: HTTP {r.status}"
        except Exception as e:  # noqa: BLE001
            err = redact(e)
    if data is None and GH_REPO:
        gh = await github_feed()
        if gh is not None:
            data, source = gh, f"github {GH_REPO}/{GH_PATH}" + (" (last good; " + GH_CACHE["err"] + ")" if GH_CACHE["err"] else "")
        elif GH_CACHE["err"]:
            err = GH_CACHE["err"]
    if data is None:
        for p in paths:
            fp = pathlib.Path(p)
            if fp.is_file():
                try:
                    data = json.loads(fp.read_text())
                    source = str(fp)
                    break
                except Exception as e:  # noqa: BLE001
                    err = f"{fp}: {e}"
    if data is None and MOCK and not os.environ.get("TD_NOFEED"):
        data = json.loads(EXAMPLE_STATUS.read_text())
        data["example"] = True
        source = "built-in EXAMPLE (mock mode)"
    elif data is None:
        # Live mode never shows invented desk data: tiles the add-on can compute
        # (Execution Trader, Risk Manager, fills in the room) come from Alpaca.
        data = {}
        source = "no desk feed (status.json not found)"
    DESK.status, DESK.status_source, DESK.status_error = data, source, err
    h.update({"last_read_ms": now_ms(), "source": source, "error": err,
              "example": bool(data.get("example"))})
    DESK.mark()


async def status_loop():
    while True:
        try:
            await read_status()
        except Exception as e:  # noqa: BLE001
            DESK.health["status_feed"]["error"] = redact(e)
        await asyncio.sleep(5)


# --------------------------------------------------------------------------
# Mock mode (local testing, no keys)
# --------------------------------------------------------------------------

MOCK_BOOK = {"AAPL": (11, 221.10), "MSFT": (6, 431.50), "NVDA": (20, 118.20),
             "XOM": (22, 114.80), "KO": (36, 69.40)}
_mock_px = {}


def mock_resync():
    rnd = random.Random(7)
    if not _mock_px:
        for s, (_, e) in MOCK_BOOK.items():
            _mock_px[s] = e * (1 + rnd.uniform(-0.03, 0.05))
    pos, mv = [], 0.0
    for s, (q, e) in MOCK_BOOK.items():
        px = _mock_px[s]
        mv += q * px
        pos.append({"symbol": s, "qty": str(q), "side": "long", "avg_entry_price": str(e),
                    "current_price": str(px), "lastday_price": str(e * 1.004)})
    cash = 14120.55
    DESK.positions = pos
    DESK.account = {"equity": str(cash + mv), "last_equity": str(cash + mv - 84.0), "cash": str(cash),
                    "buying_power": str(cash), "long_market_value": str(mv), "status": "ACTIVE",
                    "trading_blocked": False}
    DESK.orders = [{"symbol": s, "side": "sell", "type": "stop", "qty": str(q),
                    "stop_price": str(round(e * 0.9185, 2)), "status": "new"}
                   for s, (q, e) in MOCK_BOOK.items() if s != "KO"]
    if not DESK.history["t"]:
        start = dt.datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        n = max(2, min(78, int((dt.datetime.now(ET) - start).total_seconds() // 300)))
        eq, base, out_t, out_e = cash + mv - 84.0, cash + mv - 84.0, [], []
        for i in range(n):
            eq += rnd.gauss(1.2, 9)
            out_t.append(int(start.timestamp()) + i * 300)
            out_e.append(round(eq, 2))
        DESK.history = {"t": out_t, "equity": out_e}
    if not DESK.series:
        # MOCK ONLY: synthetic range series so the Main tab renders without keys.
        now_s, eq0 = int(time.time()), cash + mv
        for key, (n, step) in {"1D": (78, 300), "1W": (35, 3600), "1M": (22, 86400),
                               "1A": (252, 86400), "5A": (60, 86400)}.items():
            e, ts, es, pls = eq0, [], [], []
            for i in range(n):
                e += rnd.gauss(2, 25)
                ts.append(now_s - (n - i) * step); es.append(round(e, 2)); pls.append(round(e - eq0, 2))
            DESK.series[key] = {"t": ts, "equity": es, "profit_loss": pls, "pl": pls[-1],
                                "plpc": pls[-1] / eq0 * 100, "basis": "MOCK", "covers_from": ts[0],
                                "points": n, "scope": "mock", "error": None}
    if not DESK.fills:
        base = dt.datetime.now(dt.timezone.utc).replace(hour=14, minute=31, second=7)
        DESK.fills = [
            {"symbol": "XOM", "side": "buy", "qty": 22, "price": 114.80, "type": "fill",
             "time": (base + dt.timedelta(minutes=2)).isoformat()},
            {"symbol": "NVDA", "side": "buy", "qty": 10, "price": 118.05, "type": "partial_fill",
             "time": (base + dt.timedelta(minutes=19)).isoformat()},
            {"symbol": "NVDA", "side": "buy", "qty": 10, "price": 118.27, "type": "fill",
             "time": (base + dt.timedelta(minutes=19, seconds=4)).isoformat()},
        ]


async def mock_feed():
    h_t, h_d = DESK.health["trade_stream"], DESK.health["data_stream"]
    for h in (h_t, h_d):
        h.update({"state": "streaming", "connected_since_ms": now_ms(), "error": None})
    h_d["symbols"] = sorted(MOCK_BOOK)
    await resync("mock start")
    while True:
        await asyncio.sleep(1.0)
        s = random.choice(list(MOCK_BOOK))
        _mock_px[s] *= 1 + random.gauss(0, 0.0012)
        DESK.live_prices[s] = (_mock_px[s], now_ms())
        h_t["last_msg_ms"] = now_ms() - random.randint(0, 4000)
        h_d["last_msg_ms"] = now_ms()
        DESK.mark()


# --------------------------------------------------------------------------
# HTTP (ingress) + SSE
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Assets snapshot (read-only file, coerced; never logged)
# --------------------------------------------------------------------------
_TOP_NUM = ("account_value", "day_change", "day_change_pct", "unrealized", "unrealized_pct")
_POS_NUM = ("qty", "price", "price_chg", "mkt_val", "day_chg", "day_pct", "gain", "gain_pct")
_TOT_NUM = ("mkt_val", "day_chg", "day_pct", "gain", "gain_pct", "alloc_pct")
_ASSETS_CACHE = {}


def _fl(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def clean_assets(raw):
    """Whitelist + coerce a fed snapshot. Returns (clean|None, problem)."""
    a = raw.get("assets") if isinstance(raw, dict) and isinstance(raw.get("assets"), dict) else raw
    if not isinstance(a, dict):
        return None, "not an object"
    try:
        when = dt.datetime.fromisoformat(str(a.get("as_of")).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=ET)
    except (TypeError, ValueError):
        return None, "bad or missing as_of"
    out = {"as_of": when.isoformat(), "source": str(a.get("source") or "")[:80],
           "account": str(a.get("account") or "")[:60]}
    for k in _TOP_NUM:
        v = _fl(a.get(k))
        if v is None:
            return None, "bad " + k
        out[k] = v
    out["positions"] = []
    for p in (a.get("positions") if isinstance(a.get("positions"), list) else [])[:40]:
        if isinstance(p, dict):
            row = {"symbol": str(p.get("symbol") or "")[:12], "name": str(p.get("name") or "")[:60]}
            for k in _POS_NUM:
                row[k] = _fl(p.get(k)) or 0.0
            out["positions"].append(row)
    et = a.get("equities_total") if isinstance(a.get("equities_total"), dict) else {}
    out["equities_total"] = {k: (_fl(et.get(k)) or 0.0) for k in _TOT_NUM}
    c = a.get("cash") if isinstance(a.get("cash"), dict) else {}
    out["cash"] = {"value": _fl(c.get("value")) or 0.0, "alloc_pct": _fl(c.get("alloc_pct")) or 0.0}
    age_h = (dt.datetime.now(dt.timezone.utc) - when.astimezone(dt.timezone.utc)).total_seconds() / 3600.0
    out["age_hours"] = round(age_h, 2)
    out["stale"] = age_h > ASSETS_STALE_H
    out["stale_after_hours"] = ASSETS_STALE_H
    return out, ""


def load_assets():
    """Newest valid snapshot (by as_of) across ASSETS_PATHS (+ TD_ASSETS_FILE). Cached on (path, mtime)."""
    paths = ([os.environ["TD_ASSETS_FILE"]] if os.environ.get("TD_ASSETS_FILE") else []) + ASSETS_PATHS
    err, best = None, None
    for p in paths:
        fp = pathlib.Path(p)
        try:
            if not fp.is_file():
                continue
            st = fp.stat()
            key = (str(fp), st.st_mtime_ns)
            if st.st_size > ASSETS_MAX_BYTES:
                err = "snapshot file too large"
                continue
            if _ASSETS_CACHE.get(key) is None:
                raw = json.loads(fp.read_text())
                if not (isinstance(raw, dict) and (isinstance(raw.get("assets"), dict) or "account_value" in raw)):
                    continue        # e.g. a Life HQ feed with no assets yet
                got, why = clean_assets(raw)
                if got is None:
                    err = "snapshot rejected: " + why
                    continue
                _ASSETS_CACHE.clear()
                _ASSETS_CACHE[key] = got
            got = _ASSETS_CACHE[key]
            when = dt.datetime.fromisoformat(got["as_of"]).astimezone(dt.timezone.utc)
            if best is None or when > best[0]:
                best = (when, dict(got))
        except Exception as e:  # noqa: BLE001
            err = "snapshot unreadable: " + type(e).__name__
    if best is None:
        return {"ok": False, "assets": None, "error": err or "no snapshot file found"}
    out = best[1]
    age_h = (dt.datetime.now(dt.timezone.utc) - best[0]).total_seconds() / 3600.0
    out.update(age_hours=round(age_h, 2), stale=age_h > ASSETS_STALE_H)
    return {"ok": True, "assets": out, "error": None}


@web.middleware
async def ingress_only(request, handler):
    # Under HA, accept only the Supervisor ingress proxy (and the watchdog).
    if IN_HA and request.remote not in ("172.30.32.2", "127.0.0.1", "::1"):
        if request.path != "/api/health":
            return web.Response(status=403, text="ingress only")
    resp = await handler(request)
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def index(request):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def ui_trial(request):
    """UI-4 trial document. Production / is a different file and is not replaced."""
    return web.FileResponse(STATIC / "ui-trial" / "index.html", headers={"Cache-Control": "no-cache"})


async def ui_trial_slash(request):
    """Drop the trailing slash so relative api/ and static/ resolve at the ingress root.

    The Location is relative, not host-absolute, so the Supervisor ingress prefix stays.
    """
    return web.Response(status=302, headers={"Location": "../ui-trial", "Cache-Control": "no-store"})


async def api_state(request):
    return web.json_response(DESK.snapshot())


async def api_assets(request):
    return web.json_response(load_assets())


async def api_health(request):
    # Unauthenticated watchdog probe — no version/error details without ingress.
    return web.json_response({"ok": True})


ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")   # MA L1: validate before building Supervisor URLs


async def _read_trezor_qty():
    c = DESK.crypto
    if not IN_HA:
        q = os.environ.get("TD_TREZOR_BTC")   # local preview only
        c["btc_qty"], c["qty_error"] = (float(q) if q else None), (None if q else "not running in HA")
        return
    if not ENTITY_RE.match(TREZOR_ENTITY):
        c["btc_qty"], c["qty_error"] = None, "invalid trezor_btc_entity"
        return
    try:
        url = f"http://supervisor/core/api/states/{TREZOR_ENTITY}"
        hdr = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}
        async with DESK.session.get(url, headers=hdr) as r:
            if r.status != 200:
                c["btc_qty"], c["qty_error"] = None, f"{TREZOR_ENTITY}: HTTP {r.status}"
                return
            st = await r.json()
            c["btc_qty"], c["qty_error"] = fnum(st.get("state")), None
    except Exception as e:  # noqa: BLE001
        c["qty_error"] = redact(e)[:200]


async def _ha_state(entity):
    if not ENTITY_RE.match(entity):
        return None
    url = f"http://supervisor/core/api/states/{entity}"
    hdr = {"Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"}
    async with DESK.session.get(url, headers=hdr) as r:
        return (await r.json()) if r.status == 200 else None


async def _read_ha_prices():
    """Keyless HA price sensors (ha/packages/trading_desk_prices.yaml): sensor.price_<sym>."""
    if not IN_HA:
        return
    syms = set(DESK.held_symbols())
    for a in ((DESK.status or {}).get("holdings") or {}).get("accounts") or []:
        syms |= {p.get("symbol") for p in a.get("positions") or [] if p.get("symbol")}
    out = {}
    for sym in sorted(syms) + ["BTC-USD"]:
        try:
            st = await _ha_state("sensor.price_" + sym.lower().replace("-", "_"))
        except Exception:  # noqa: BLE001
            st = None
        v = fnum((st or {}).get("state"))
        if v is not None:
            at = st.get("attributes") or {}
            out[sym] = {"price": v, "prev_close": fnum(at.get("chartPreviousClose") or at.get("previousClose")),
                        "updated": st.get("last_updated")}
    DESK.ha_prices = out


async def crypto_loop():
    """BTC spot + 24 h open from Coinbase's public endpoints (GET, no key)."""
    c = DESK.crypto
    while True:
        await _read_trezor_qty()
        await _read_ha_prices()
        try:
            async with DESK.session.get(BTC_SPOT_URL) as r:
                r.raise_for_status()
                c["btc_price"] = fnum(((await r.json()).get("data") or {}).get("amount"))
            async with DESK.session.get(BTC_STATS_URL, headers={"User-Agent": "ha-trading-desk"}) as r:
                r.raise_for_status()
                c["btc_open_24h"] = fnum((await r.json()).get("open"))
            c["price_source"], c["price_as_of_ms"], c["price_error"] = "Coinbase public spot", now_ms(), None
        except Exception as e:  # noqa: BLE001
            c["price_error"] = redact(e)[:200]
        DESK.mark()
        await asyncio.sleep(CRYPTO_POLL_S)


async def api_history(request):
    """Main-tab chart series for one range (Alpaca account only)."""
    key = (request.query.get("range") or "1D").upper()
    if key not in RANGES:
        return web.json_response({"error": "unknown range"}, status=400)
    return web.json_response({"range": key, **(DESK.series.get(key) or {"error": "not loaded yet", "scope": "alpaca"})})


def levels(sym):
    """Entry price and resting stop prices for the chart's horizontal lines."""
    entry = next((fnum(p.get("avg_entry_price")) for p in DESK.positions if p.get("symbol") == sym), None)
    stops = sorted({fnum(o.get("stop_price")) for o in DESK.flat_orders()
                    if o.get("symbol") == sym and o.get("type") in ("stop", "stop_limit", "trailing_stop")
                    and fnum(o.get("stop_price")) is not None})
    return {"entry": entry, "stops": stops}


async def api_bars(request):
    """Price chart data for one held symbol (GET /v2/stocks/{sym}/bars)."""
    sym = (request.query.get("symbol") or "").upper()
    if sym not in DESK.held_symbols():
        return web.json_response({"error": "symbol not held"}, status=400)
    if MOCK:
        rnd = random.Random(sym)
        px, t0, out = _mock_px.get(sym, 100), int(time.time()) - 78 * 300, []
        for i in range(78):
            o = px
            px *= 1 + rnd.gauss(0, 0.002)
            out.append({"t": t0 + i * 300, "o": round(o, 2), "h": round(max(o, px) * 1.001, 2),
                        "l": round(min(o, px) * 0.999, 2), "c": round(px, 2), "vw": round((o + px) / 2, 2)})
        return web.json_response({"symbol": sym, "bars": out, **levels(sym)})
    start = dt.datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)
    try:
        data = await rest_get(DATA_REST, f"/v2/stocks/{sym}/bars",
                              {"timeframe": "5Min", "start": start.astimezone(dt.timezone.utc).isoformat(),
                               "feed": FEED, "limit": "500"})
        bars = [{"t": int(dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()),
                 "o": b.get("o"), "h": b.get("h"), "l": b.get("l"), "c": b.get("c"), "vw": b.get("vw")}
                for b in data.get("bars") or []]
        return web.json_response({"symbol": sym, "bars": bars, **levels(sym)})
    except Exception as e:  # noqa: BLE001
        return web.json_response({"error": redact(e)}, status=502)


async def api_events(request):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                       "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    q = asyncio.Queue(maxsize=2)
    DESK.clients.add(q)
    try:
        await resp.write(f"event: state\ndata: {json.dumps(DESK.snapshot())}\n\n".encode())
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"event: state\ndata: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        DESK.clients.discard(q)
    return resp


async def broadcaster():
    while True:
        await DESK.dirty.wait()
        DESK.dirty.clear()
        if DESK.clients:
            payload = json.dumps(DESK.snapshot())
            for q in list(DESK.clients):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(payload)
        await asyncio.sleep(0.5)   # at most 2 pushes per second


async def on_startup(app):
    DESK.session = make_session()
    tasks = [broadcaster(), status_loop(), crypto_loop()]
    if MOCK:
        tasks.append(mock_feed())
    elif KEY_ID and SECRET:
        tasks += [trade_stream(), resync_loop(), price_poll_loop()]
        if DATA_STREAM_ON:
            tasks.append(data_stream())
        DESK.resync_now.set()
    else:
        for k in ("trade_stream", "data_stream"):
            DESK.health[k].update({"state": "no keys", "error": "Add API key and secret in the add-on Configuration tab"})
        DESK.health["rest"]["error"] = "no API keys configured"
        log.warning("No Alpaca API keys configured; dashboard runs with desk feed only.")
    app["tasks"] = [asyncio.create_task(t) for t in tasks]


async def on_cleanup(app):
    for t in app.get("tasks", []):
        t.cancel()
    await DESK.session.close()


def build_app():
    app = web.Application(middlewares=[ingress_only])
    app.router.add_get("/", index)
    app.router.add_get("/ui-trial", ui_trial)
    app.router.add_get("/ui-trial/", ui_trial_slash)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/health", api_health)
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/bars", api_bars)
    app.router.add_get("/api/assets", api_assets)
    app.router.add_get("/api/history", api_history)
    app.router.add_static("/static/", STATIC, append_version=False)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    global DESK
    problems = selfcheck.scan()
    if problems:
        log.error("read-only self-check failed; refusing to start:\n%s", "\n".join(problems))
        sys.exit(2)
    DESK = Desk()
    log.info("Trading Desk %s starting (env=%s feed=%s mock=%s keys=%s) on :%d",
             VERSION, ENV, FEED, MOCK, "set" if (KEY_ID and SECRET) else "missing", PORT)
    web.run_app(build_app(), host="0.0.0.0", port=PORT, access_log=None, print=None)


if __name__ == "__main__":
    main()
