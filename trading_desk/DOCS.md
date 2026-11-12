# Trading Desk

**Main tab (0.2.0+)**: plain-English totals, gains toggle and holdings. **Detail** has risk, stops, orders, feed status, bots.
Data availability: Day = Schwab + Alpaca + BTC (24 h). Week/Month/Year/5Y = Alpaca account only, net of deposits,
and only since the account opened, so Year and 5Y are labelled with their real start date. No Schwab or
Trezor history source exists (feed has none; HA recorder holds no desk sensors yet).
Install `ha/packages/trading_desk_prices.yaml` into `/config/packages/` for keyless price sensors and the Trezor helper.

Total assets strip (0.1.13+): combined Schwab+Alpaca at the top of Desk, with Day/Week/Month/Year/Unrealized. Week/Month/Year are Alpaca portfolio-history until the Schwab feed grows period fields.


A read-only, live control room for an Alpaca equity desk, in the Home Assistant
sidebar (Ingress, behind the HA login, no ports exposed).

## Schwab holdings file (0.3.5)

Share counts live in `/share/trading_desk/schwab_holdings.json` (read through the `share:ro` map; the add-on never writes it). That path is a constant, not an option. A local preview can point `TD_SCHWAB_FILE` at another file. Schema `trading_desk.schwab_holdings/v1`. Symbols with `"quote": "alpaca"` are priced from Alpaca; a mutual fund can use `"quote": "static"` with `static_price`. The Schwab tab and the Main Schwab total use those live prices. If the file is missing or invalid, Main keeps the desk-feed holdings numbers.

Example (placeholder names and round amounts only):

```json
{"schema":"trading_desk.schwab_holdings/v1","as_of":"2026-01-02T09:00:00-05:00","source":"example",
 "accounts":[
  {"id":"individual","name":"Brokerage","mask":"0000","cash":100.00,"positions":[
    {"symbol":"AAA","name":"Example A Corp","type":"equity","qty":10,"cost_basis":1000.00,"quote":"alpaca"},
    {"symbol":"BBB","name":"Example B Inc","type":"equity","qty":5.5,"cost_basis":null,"quote":"alpaca"}]},
  {"id":"roth","name":"Retirement","cash":25.00,"positions":[
    {"symbol":"AAA","name":"Example A Corp","type":"equity","qty":2,"cost_basis":150.00,"quote":"alpaca"},
    {"symbol":"FUNDX","name":"Example Index Fund","type":"mutual_fund","qty":3,"cost_basis":null,"quote":"static",
     "static_price":50.00,"static_price_as_of":"2026-01-02T09:00:00-05:00"}]}]}
```

`GET api/schwab` returns the same object as `schwab` on `api/state` and the event stream.

## Bubble charts (0.3.6)

The Main tab draws two charts above the holdings table. Gain/Loss uses unrealized dollars (X) and percent of cost (Y). Positions uses today's change in market value (X) and today's percent price change (Y). Bubble area follows market value. Green is a gain and red is a loss. Zero is neutral, on the origin.

Each open row is its own bubble: every Schwab account position, every Alpaca position, and Trezor Bitcoin when the helper has a value. AAA in a Schwab account and AAA at Alpaca stay two bubbles. Cash is not a bubble. The Gain/Loss header sums unrealized dollars for rows with a known cost and divides by the sum of those costs. Rows without a cost, including Trezor and shorts, are left out and named in Info. The Positions header sums today's dollar change for rows that have one. Its percent divides that sum by the prior value of those rows. When the account reports cash, that cash is included in the prior value, the same way the Schwab total includes cash, and Info says so. A static fund such as FUNDX with no day change is left off the Positions chart and can still appear on Gain/Loss when its cost is known.

The choice of chart is stored in the browser session only. There is no new option. Updated is an America/New_York clock labeled ET. The charts do not place, change, or cancel orders. Options, keys, and `/share/trading_desk/schwab_holdings.json` are unchanged. If that file is missing, Schwab rows still fall back to the desk feed, and Alpaca rows still render.

## UI-4 page (0.3.6)

`/` is the UI-4 page. `/ui-trial` serves the same document. `/ui-trial/` redirects with relative `Location: ../` (the ingress root).
`/legacy` is the previous page, kept for rollback. `/legacy/` redirects to `../legacy`.
Same GET endpoints (`api/state`, `api/events`, `api/bars`, `api/history`, `api/assets`, `api/schwab`).
The page cannot place, change, or cancel orders. Options and the schema are unchanged.

## Configuration

| option | default | |
|---|---|---|
| `api_key_id` | — | Alpaca API key ID (**read-only key required**) |
| `api_secret_key` | — | Alpaca API secret (password field) |
| `trading_env` | `live` | `live` = api.alpaca.markets, `paper` = paper-api |
| `data_feed` | `iex` | market-data websocket feed: `iex` (free), `sip` (paid plan), `delayed_sip` |
| `data_stream` | `true` | hold the market-data websocket. If another client already holds Alpaca's single data connection (error 406), the add-on backs off and polls latest trades over REST every 15 s instead. |
| `status_file` | — | path of the desk status JSON (see `docs/STATUS_FEED_SPEC.md`) |
| `status_entity` | — | or an HA entity whose attributes carry the status document |
| `temp_open_risk_cap_pct` | — | override the TEMP open-risk cap (% equity). Unset → from feed or "unknown" |
| `single_name_cap_pct` | — | override the single-name cap (% equity) |
| `mock` | `false` | simulated data for testing; the UI shows MOCK DATA |

Keys are never logged; key and secret strings are redacted from every log line and error.

## Read-only guarantee

* The only outbound HTTP method is GET; a request hook raises on anything else.
* No order or position endpoints are called except `GET /v2/orders?status=open` (to read stops).
* `selfcheck.py` scans the shipped source for POST/PUT/PATCH/DELETE calls, write
  routes and order submit/replace/cancel/close helpers. It runs during the image
  build (the build fails) and at every start (the add-on refuses to start).

**Required:** configure a **read-only** Alpaca API key (Alpaca dashboard → API keys → "Trading permissions: Read only"). Do **not** use a full-access key. `trading_env: live` only selects the live API host; it does not grant trade rights when the key is read-only. A full-access key can trade even though this add-on only sends GET — see INSTALL_LOCAL.md.

## Desk feed from GitHub (0.1.3+)

The desk publishes `status.json` to a private repo every 15 min (only when it changed).
Set `feed_github_repo: bcart180/trading-desk-feed`, `feed_github_path: status.json`, and
`feed_github_token` to a fine-grained PAT with **read-only Contents** on that one repo.
The add-on polls with ETag caching (minimum 60 s), keeps the last good copy if GitHub is
unreachable, and otherwise falls back to the local status file. The token is never logged.

## Assets tab (0.1.10+)

Shows a manually fed brokerage snapshot (no login, no scraping). The add-on reads, never writes.
It uses the newest valid snapshot (by `as_of`) from:

1. `/share/trading_desk/assets.json` (a bare assets object or `{"assets": {...}}`), or
2. the `assets` key of the Life HQ private feed `/homeassistant/pyscript/.life_hq_feed.json`
   (updated by the existing Life HQ push, so one push feeds both views).

Required: `as_of` (ISO time), `account_value`, `day_change`, `day_change_pct`, `unrealized`, `unrealized_pct`.
Optional: `account`, `source`, `positions[]`, `equities_total`, `cash`. The badge turns STALE after 6 hours.