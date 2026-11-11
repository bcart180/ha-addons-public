"""Schwab holdings file: validate, price, and roll up. Read-only.

The file lives at a constant path (not an add-on option). This module never
writes it. Prices come from the caller (Alpaca snapshots, trades, clock).
"""
import datetime as dt
import json
import math
import os
import pathlib
import re
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCHEMA = "trading_desk.schwab_holdings/v1"
PATH = "/share/trading_desk/schwab_holdings.json"
MAX_BYTES = 64 * 1024
ID_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
SYM_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MASK_RE = re.compile(r"^\d{0,4}$")
TYPES = {"equity", "etf", "mutual_fund", "other"}
_CACHE = {}

# Placeholder only. Mock mode uses this when no file is present.
MOCK_DOC = {
    "schema": SCHEMA,
    "as_of": "2026-01-02T09:00:00-05:00",
    "source": "example",
    "accounts": [
        {
            "id": "individual",
            "name": "Brokerage",
            "mask": "0000",
            "cash": 100.00,
            "positions": [
                {"symbol": "AAA", "name": "Example A Corp", "type": "equity",
                 "qty": 10, "cost_basis": 1000.00, "quote": "alpaca"},
                {"symbol": "BBB", "name": "Example B Inc", "type": "equity",
                 "qty": 5.5, "cost_basis": None, "quote": "alpaca"},
            ],
        },
        {
            "id": "roth",
            "name": "Retirement",
            "cash": 25.00,
            "positions": [
                {"symbol": "AAA", "name": "Example A Corp", "type": "equity",
                 "qty": 2, "cost_basis": 150.00, "quote": "alpaca"},
                {"symbol": "FUNDX", "name": "Example Index Fund", "type": "mutual_fund",
                 "qty": 3, "cost_basis": None, "quote": "static",
                 "static_price": 50.00, "static_price_as_of": "2026-01-02T09:00:00-05:00"},
            ],
        },
    ],
}


def clear_cache():
    _CACHE.clear()


def holdings_path():
    return os.environ.get("TD_SCHWAB_FILE") or PATH


def finite(x):
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_time(value):
    """ISO date-time or epoch. A naive value is read as ET. None if it is not a time."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        when = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return None
        sec = float(value) / 1000.0 if abs(value) > 1e12 else float(value)
        try:
            when = dt.datetime.fromtimestamp(sec, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            when = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ET)
    return when


def ts_ms(value, now_ms=None):
    when = parse_time(value)
    if when is None:
        return now_ms
    return int(when.timestamp() * 1000)


def _et(when):
    return when.astimezone(ET) if ET is not None else when


def fmt_et_clock(when):
    local = _et(when)
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return "%d:%02d %s ET" % (hour, local.minute, ampm)


def fmt_et_date(when):
    local = _et(when)
    return "%s %d, %d ET" % (local.strftime("%b"), local.day, local.year)


def fmt_et_daytime(when):
    local = _et(when)
    return "%s %s" % (local.strftime("%a"), fmt_et_clock(when))


class FetchGate:
    """At most one Alpaca fetch every 60s, plus one when the file changes."""

    def __init__(self):
        self.last = None

    def due(self, now, changed):
        if self.last is None or changed or (now - self.last) >= 60:
            self.last = now
            return True
        return False


def clean(raw):
    """Whitelist and coerce. Returns (doc, reason). Never raises."""
    try:
        return _clean(raw)
    except Exception:  # noqa: BLE001 — a bad file is a result, not a crash
        return None, "unreadable"


def _clean(raw):
    if not isinstance(raw, dict):
        return None, "not an object"
    if raw.get("schema") != SCHEMA:
        return None, "wrong schema"
    when = parse_time(raw.get("as_of"))
    if when is None or not isinstance(raw.get("as_of"), str):
        return None, "bad as_of"
    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 8:
        return None, "bad accounts"
    source = raw.get("source") if isinstance(raw.get("source"), str) else ""
    out_accounts = []
    for acct in accounts:
        cleaned, why = _account(acct)
        if cleaned is None:
            return None, why
        out_accounts.append(cleaned)
    return {
        "schema": SCHEMA,
        "as_of": when.isoformat(),
        "source": source[:80],
        "accounts": out_accounts,
    }, ""


def _account(acct):
    if not isinstance(acct, dict):
        return None, "bad account"
    aid = acct.get("id")
    if not isinstance(aid, str) or not ID_RE.match(aid):
        return None, "bad account id"
    name = acct.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 40:
        return None, "bad account name"
    mask = None
    if "mask" in acct and acct.get("mask") is not None:
        mask = acct.get("mask")
        if not isinstance(mask, str) or not MASK_RE.match(mask):
            return None, "bad mask"
        if mask == "":
            mask = None
    if "cash" in acct and acct.get("cash") is not None:
        cash = finite(acct.get("cash"))
        if cash is None or cash < 0:
            return None, "bad cash"
    else:
        cash = 0.0
    positions = acct.get("positions") if "positions" in acct else []
    if not isinstance(positions, list) or len(positions) > 40:
        return None, "bad positions"
    rows = []
    for pos in positions:
        row, why = _position(pos)
        if row is None:
            return None, why
        rows.append(row)
    return {"id": aid, "name": name.strip(), "mask": mask, "cash": cash, "positions": rows}, ""


def _position(pos):
    if not isinstance(pos, dict):
        return None, "bad position"
    symbol = pos.get("symbol")
    if not isinstance(symbol, str) or not SYM_RE.match(symbol):
        return None, "bad symbol"
    name = pos.get("name") if isinstance(pos.get("name"), str) else ""
    if len(name) > 60:
        return None, "bad position name"
    kind = pos.get("type")
    if kind not in TYPES:
        return None, "bad type"
    qty = finite(pos.get("qty"))
    if qty is None or qty <= 0:
        return None, "bad qty"
    if "cost_basis" in pos and pos.get("cost_basis") is not None:
        basis = finite(pos.get("cost_basis"))
        if basis is None or basis < 0:
            return None, "bad cost_basis"
    else:
        basis = None
    quote = pos.get("quote")
    if quote not in ("alpaca", "static"):
        return None, "bad quote"
    row = {
        "symbol": symbol,
        "name": name[:60],
        "type": kind,
        "qty": qty,
        "cost_basis": basis,
        "quote": quote,
    }
    if quote == "static":
        price = finite(pos.get("static_price"))
        as_of = parse_time(pos.get("static_price_as_of"))
        if price is None or price <= 0 or as_of is None or not isinstance(pos.get("static_price_as_of"), str):
            return None, "static row without price"
        row["static_price"] = price
        row["static_price_as_of"] = as_of.isoformat()
    return row, ""


def load_file(path=None):
    """Read and validate one file. Cached by (path, mtime_ns). Never raises."""
    fp = pathlib.Path(path or holdings_path())
    try:
        st = fp.stat()
    except FileNotFoundError:
        return {"ok": False, "error": "no holdings file", "holdings": None, "key": None}
    except OSError:
        return {"ok": False, "error": "unreadable", "holdings": None, "key": None}
    key = (str(fp), st.st_mtime_ns)
    if st.st_size > MAX_BYTES:
        return {"ok": False, "error": "holdings file too large", "holdings": None, "key": key}
    hit = _CACHE.get(key)
    if hit is not None:
        return {"ok": hit["ok"], "error": hit["error"], "holdings": hit["holdings"], "key": key}
    try:
        raw = json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        rec = {"ok": False, "error": "bad JSON", "holdings": None}
        _CACHE.clear()
        _CACHE[key] = rec
        return {"ok": False, "error": "bad JSON", "holdings": None, "key": key}
    except OSError:
        return {"ok": False, "error": "unreadable", "holdings": None, "key": key}
    doc, why = clean(raw)
    if doc is None:
        rec = {"ok": False, "error": why, "holdings": None}
    else:
        rec = {"ok": True, "error": None, "holdings": doc}
    _CACHE.clear()
    _CACHE[key] = rec
    return {"ok": rec["ok"], "error": rec["error"], "holdings": rec["holdings"], "key": key}


def choose_price(stream, rest, snap, quote):
    """Newest trade wins. A two-sided quote is the mid when no trade exists.

    Each trade is (price, ts_ms) or None. quote is {bp, ap, ts} or None.
    Returns (price, ts_ms, source).
    """
    ranked = []
    for rank, item in ((3, stream), (2, rest), (1, snap)):
        if not item:
            continue
        price, ts = item[0], item[1]
        px = finite(price)
        if px is None or px <= 0:
            continue
        stamp = ts if isinstance(ts, (int, float)) and math.isfinite(float(ts)) else 0
        ranked.append((stamp, rank, px))
    if ranked:
        stamp, _rank, px = max(ranked)
        return px, int(stamp), "alpaca trade"
    if quote:
        bp, ap = finite(quote.get("bp")), finite(quote.get("ap"))
        if bp is not None and ap is not None and bp > 0 and ap > 0:
            ts = quote.get("ts")
            stamp = int(ts) if isinstance(ts, (int, float)) and math.isfinite(float(ts)) else 0
            return (bp + ap) / 2.0, stamp, "alpaca quote mid"
    return None, None, "none"


def session_uses_prev_daily(daily_ts, clock):
    """True when prev close is prevDailyBar: market open, or after today's close."""
    if not clock:
        return False
    if clock.get("is_open"):
        return True
    now = parse_time(clock.get("timestamp") or clock.get("as_of"))
    if now is None:
        return False
    now = _et(now)
    nxt = parse_time(clock.get("next_open"))
    if nxt is not None:
        nxt = _et(nxt)
        if nxt.date() == now.date() and nxt > now:
            return False  # pre-market: today's session has not printed a daily bar
    bar = parse_time(daily_ts) if daily_ts is not None else None
    if bar is not None and _et(bar).date() == now.date():
        return True  # after today's close the daily bar is today's session
    return False  # weekend, holiday, or a daily bar from another date


def prev_close(daily, prev, clock):
    """daily and prev are (close, timestamp) or None."""
    daily_ts = daily[1] if daily else None
    use_prev = session_uses_prev_daily(daily_ts, clock)
    chosen = prev if use_prev else daily
    if not chosen:
        return None
    px = finite(chosen[0])
    if px is None or px <= 0:
        return None
    return px


def _scrub(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _iso(ms):
    if ms is None:
        return None
    try:
        return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _label(source, ts_ms, is_open, now_ms, static_as_of):
    if source == "snapshot":
        when = parse_time(static_as_of)
        return "snapshot " + (fmt_et_date(when) if when else "ET")
    if source == "none":
        return "no quote"
    if is_open is True:
        age = ((now_ms - ts_ms) / 1000.0) if (now_ms is not None and ts_ms) else 1e9
        if source == "alpaca trade" and age < 120:
            return "live"
        return "delayed"
    if is_open is False and ts_ms:
        when = dt.datetime.fromtimestamp(ts_ms / 1000.0, dt.timezone.utc)
        return "closed · last trade " + fmt_et_clock(when)
    return "delayed"


def _row_view(pos, trades, quotes, bars, clock, now_ms, is_open):
    symbol = pos["symbol"]
    book = trades.get(symbol) or {}
    if pos["quote"] == "static":
        price = pos["static_price"]
        stamp = ts_ms(pos.get("static_price_as_of"), now_ms)
        source = "snapshot"
        prev = None
        day_pl = None
        day_plpc = None
    else:
        price, stamp, source = choose_price(
            book.get("stream"), book.get("rest"), book.get("snap"), quotes.get(symbol))
        bar = bars.get(symbol) or {}
        prev = prev_close(bar.get("daily"), bar.get("prev"), clock) if source != "none" else None
        if price is None or prev is None:
            day_pl = None
            day_plpc = None
        else:
            day_pl = pos["qty"] * (price - prev)
            day_plpc = ((price - prev) / prev * 100.0) if prev else None
    value = (pos["qty"] * price) if price is not None else None
    basis = pos.get("cost_basis")
    if value is None or basis is None:
        unrl = None
        unrl_pc = None
    else:
        unrl = value - basis
        unrl_pc = (unrl / basis * 100.0) if basis else None
    age = None
    if stamp is not None and now_ms is not None and source != "none":
        age = max(0, int((now_ms - stamp) / 1000))
    return {
        "symbol": symbol,
        "name": pos.get("name") or "",
        "type": pos.get("type"),
        "qty": pos["qty"],
        "cost_basis": basis,
        "quote": pos["quote"],
        "price": price,
        "prev_close": prev,
        "value": value,
        "day_pl": day_pl,
        "day_plpc": day_plpc,
        "unrealized_pl": unrl,
        "unrealized_plpc": unrl_pc,
        "price_source": source,
        "price_ts": pos.get("static_price_as_of") if source == "snapshot" else _iso(stamp),
        "price_age_s": age,
        "label": _label(source, stamp, is_open, now_ms, pos.get("static_price_as_of")),
    }


def _rollup(rows, cash):
    priced = [r for r in rows if r["value"] is not None]
    positions_value = sum(r["value"] for r in priced)
    total = positions_value + cash
    day_rows = [r for r in rows if r["day_pl"] is not None]
    missing = [r["symbol"] for r in rows if r["value"] is None]
    day_excluded = [r["symbol"] for r in rows if r["value"] is not None and r["day_pl"] is None]
    basis_excluded = [r["symbol"] for r in rows if r["cost_basis"] is None]
    excluded_value = sum(r["value"] for r in rows if r["value"] is not None and r["day_pl"] is None)
    if day_rows:
        day_pl = sum(r["day_pl"] for r in day_rows)
        base = total - day_pl - excluded_value
        day_plpc = (day_pl / base * 100.0) if base else None
    else:
        day_pl = None
        day_plpc = None
    basis_rows = [r for r in rows if r["unrealized_pl"] is not None and r["cost_basis"] is not None]
    if basis_rows:
        unrl = sum(r["unrealized_pl"] for r in basis_rows)
        basis = sum(r["cost_basis"] for r in basis_rows)
        unrl_pc = (unrl / basis * 100.0) if basis else None
    else:
        unrl = None
        unrl_pc = None
    return {
        "positions_value": positions_value,
        "cash": cash,
        "total": total,
        "day_pl": day_pl,
        "day_plpc": day_plpc,
        "unrealized_pl": unrl,
        "unrealized_plpc": unrl_pc,
        "missing": missing,
        "day_excluded": day_excluded,
        "basis_excluded": basis_excluded,
    }


def build_view(doc, trades, quotes, bars, clock, now_ms, feed, reason, keys_configured):
    """Per-row, per-account, and combined totals. Null-safe."""
    if not doc:
        return _scrub({"ok": False, "error": reason or "no holdings file", "accounts": [], "combined": None,
                       "market": _market(clock), "feed": feed, "prices_note": None})
    is_open = bool(clock.get("is_open")) if clock else None
    if clock is None:
        is_open = None
    accounts = []
    flat = []
    for acct in doc.get("accounts") or []:
        rows = [_row_view(p, trades or {}, quotes or {}, bars or {}, clock, now_ms, is_open)
                for p in acct.get("positions") or []]
        roll = _rollup(rows, finite(acct.get("cash")) or 0.0)
        accounts.append({
            "id": acct.get("id"),
            "name": acct.get("name"),
            "mask": acct.get("mask") or None,
            "positions": rows,
            **roll,
        })
        flat.extend(rows)
    cash = sum((a["cash"] or 0.0) for a in accounts)
    combined = _rollup(flat, cash)
    alpaca_rows = [r for r in flat if r["quote"] == "alpaca"]
    priced = [r for r in alpaca_rows if r["price"] is not None]
    note = None
    if alpaca_rows and not keys_configured:
        note = "No live prices: " + (reason or "no API keys configured")
    elif alpaca_rows and not priced:
        why = (reason or "").strip()
        note = "No live prices" + ((": " + why) if why else "")
    newest = None
    for row in flat:
        if row["quote"] == "alpaca" and row["price_ts"]:
            when = parse_time(row["price_ts"])
            if when is not None and (newest is None or when > newest):
                newest = when
    return _scrub({
        "ok": True,
        "error": None,
        "schema": doc.get("schema"),
        "as_of": doc.get("as_of"),
        "source": doc.get("source") or "",
        "feed": feed,
        "price_as_of": newest.isoformat() if newest else None,
        "prices_note": note,
        "market": _market(clock),
        "accounts": accounts,
        "combined": combined,
    })


def _market(clock):
    if not clock:
        return {"is_open": None, "next_open": None, "next_close": None, "as_of": None}
    return {
        "is_open": bool(clock.get("is_open")),
        "next_open": clock.get("next_open"),
        "next_close": clock.get("next_close"),
        "as_of": clock.get("timestamp") or clock.get("as_of"),
    }


def alpaca_symbols(doc):
    if not doc:
        return []
    found = []
    seen = set()
    for acct in doc.get("accounts") or []:
        for pos in acct.get("positions") or []:
            if pos.get("quote") == "alpaca" and pos.get("symbol") and pos["symbol"] not in seen:
                seen.add(pos["symbol"])
                found.append(pos["symbol"])
    return sorted(found)


def ingest_snapshots(desk, payload, now_ms):
    """Store snapshot trades, quotes, and daily bars. Does not log prices."""
    if not isinstance(payload, dict):
        return
    snaps = payload.get("snapshots") if isinstance(payload.get("snapshots"), dict) else payload
    if not isinstance(snaps, dict):
        return
    for sym, snap in snaps.items():
        if not isinstance(snap, dict) or not isinstance(sym, str):
            continue
        trade = snap.get("latestTrade") if isinstance(snap.get("latestTrade"), dict) else {}
        px = finite(trade.get("p"))
        if px is not None and px > 0:
            desk.trades.setdefault(sym, {})["snap"] = (px, ts_ms(trade.get("t"), now_ms))
        quote = snap.get("latestQuote") if isinstance(snap.get("latestQuote"), dict) else {}
        bp, ap = finite(quote.get("bp")), finite(quote.get("ap"))
        if bp is not None and ap is not None and bp > 0 and ap > 0:
            desk.quotes[sym] = {"bp": bp, "ap": ap, "ts": ts_ms(quote.get("t"), now_ms)}
        daily = snap.get("dailyBar") if isinstance(snap.get("dailyBar"), dict) else {}
        prev = snap.get("prevDailyBar") if isinstance(snap.get("prevDailyBar"), dict) else {}
        dc, pc = finite(daily.get("c")), finite(prev.get("c"))
        desk.bars[sym] = {
            "daily": (dc, daily.get("t")) if dc is not None else None,
            "prev": (pc, prev.get("t")) if pc is not None else None,
        }
