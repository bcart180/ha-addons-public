# Trading Desk

**Main tab (0.2.0+)**: plain-English totals, gains toggle and holdings. **Detail** has risk, stops, orders, feed status, bots.
Data availability: Day = Schwab + Alpaca + BTC (24 h). Week/Month/Year/5Y = Alpaca account only, net of deposits,
and only since the account opened, so Year and 5Y are labelled with their real start date. No Schwab or
Trezor history source exists (feed has none; HA recorder holds no desk sensors yet).
Install `ha/packages/trading_desk_prices.yaml` into `/config/packages/` for keyless price sensors and the Trezor helper.

Total assets strip (0.1.13+): combined Schwab+Alpaca at the top of Desk, with Day/Week/Month/Year/Unrealized. Week/Month/Year are Alpaca portfolio-history until the Schwab feed grows period fields.


A read-only, live control room for an Alpaca equity desk, in the Home Assistant
sidebar (Ingress, behind the HA login, no ports exposed).

## UI-4 page (0.3.4)

`/` is the UI-4 page. `/ui-trial` serves the same document. `/ui-trial/` redirects with relative `Location: ../` (the ingress root).
`/legacy` is the previous page, kept for rollback. `/legacy/` redirects to `../legacy`.
Same GET endpoints (`api/state`, `api/events`, `api/bars`, `api/history`, `api/assets`).
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