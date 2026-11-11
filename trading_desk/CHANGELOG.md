# Changelog

## 0.3.5 — 2026-11-11

- Schwab tab prices a static holdings file at `/share/trading_desk/schwab_holdings.json` from Alpaca live trades (GET snapshots and clock only). The desk feed stays the fallback when that file is missing.
- Detail no longer rebuilds its tables on every price tick, so iOS WebKit does not jump to the bottom.
- Options and schema are unchanged. Still GET and EventSource only.

## 0.3.4 — 2026-11-10

- UI-4 is the production page at `/`. `/ui-trial` serves the same page. `/ui-trial/` redirects with a relative `Location: ../` to the ingress root.
- The previous page stays at `/legacy` for rollback. `/legacy/` redirects to `../legacy` so the old relative paths still resolve at the ingress root.
- Options, schema, keys, feeds, and risk caps are unchanged. Still GET and EventSource only.

## 0.3.3 — 2026-11-10

- UI-4 trial at `/ui-trial` on the same add-on. Black and charcoal surfaces, light type, Tesla blue `#3e6ae1` for the selected tab and range. Production `/` and its static files are unchanged.
- The version bump is required so Home Assistant will ship the new route. `/ui-trial` is registered in the add-on process; a static file alone cannot be that path. Options, schema, keys, feeds, and risk caps are unchanged.
- The trial keeps the nine controls and every existing data surface. It uses GET and EventSource only. It cannot place, change, or cancel orders.
- Allocation swatches are labeled: Schwab cool gray, Alpaca blue, Bitcoin violet. Orange and amber are not used.

## 0.3.2 — 2026-11-06

- Presentation only: calm 2022 Model S Plaid surfaces (near-black cards, hairline borders, light-weight figures, quiet pills). Green and red stay on gain/loss and status. No data, order, or API changes. Still read-only.
- Main-tab chart axis and grid use the same dark hairline colors (presentation constants only).

## 0.3.1 — 2026-11-02

- Remove '(new)' name suffix; no functional change.

## 0.3.0 — 2026-11-01

- Public store marker: name suffix (new); no functional change.

## 0.2.3
- Fix (L1): cost basis is converted with `Number()` before use, so numeric strings from the feed can't break the sum.
- Fix (L2): short rows (qty < 0) show "basis n/a" and are excluded from the unrealized gain/loss headline.
- Security: secrets the add-on itself writes to logs or API errors (Alpaca key ID/secret, GitHub feed token, Supervisor token) are masked to the last 4 characters only. `api_key_id`, `api_secret_key` and `feed_github_token` remain `password?` in the schema (option names unchanged).
- Note: Home Assistant Supervisor's own add-on info read-back still returns raw option values; the add-on cannot mask that.

## 0.2.2
- Main tab: unrealized gain/loss headline at the top (Schwab + Alpaca, $ and %, vs. real cost basis from Alpaca positions and the Schwab feed).
- Holdings table: per-holding gain/loss $ and % column.
- Holdings without a known cost basis (e.g. the Trezor BTC, funds the feed reports without basis) show "basis n/a" and are excluded from the headline, with the exclusion listed.

## 0.2.1
- Fix: Supervisor token was not visible to the app under s6 (clean env), so the Trezor helper and HA price sensors read as "not running in HA". Load it from s6 container_environment.

## 0.2.0
- New **Main** tab (default, House Soft Slate theme, big plain-English labels): everything you own
  (Schwab + Alpaca + Trezor BTC), allocation bar, Day / Week / Month / Year / 5Y gain toggle, Alpaca value chart,
  one table of every holding with price, today %, shares, value.
- Old Desk view is now the **Detail** tab (`#detail`); Assets tab renamed **Schwab**.
- Fix (misleading): Week/Month/Year counted deposits as gain; now uses Alpaca `profit_loss` (net of deposits),
  falling back to equity change only with a label.
- New read-only `GET /api/history?range=1D|1W|1M|1A|5A` (Alpaca portfolio history; each range fetched
  separately so a rejected period cannot break resync). Ranges shorter than requested are labelled with the real start date.
- Trezor BTC: quantity from HA helper `input_number.trezor_btc` (option `trezor_btc_entity`), valued at Coinbase public spot (GET, no key).
- Prices from HA keyless sensors `sensor.price_<sym>` override feed prices on Main when present.
- `ha/packages/trading_desk_prices.yaml`: keyless REST price sensors (Yahoo chart endpoint for stocks, Coinbase for BTC),
  5-minute market-hours poll automation, Trezor helper, Trezor value template. Replaces the Life HQ price routine.
- Schwab and Trezor have no history source: Week+ ranges are Alpaca-only and say so.

## 0.1.13
- Simplify Desk: **Total assets** strip at the top (Schwab + Alpaca combined).
- Period gains: Day (Schwab+Alpaca when both available), Week / Month / Year (Alpaca portfolio history), Unrealized (combined).
- Alpaca daily equity curve stays granular (5Min). Week/Month/Year come from Alpaca `/v2/account/portfolio/history`.
- Schwab week/month/year not in the desk feed yet — strip notes the gap; Day + Unrealized still combine both books.

## 0.1.11
- Fix: Whole Portfolio panel was squeezed into a one-column strip (missing `.portfolio` grid rule, present since 0.1.9). It now spans the full width with proper columns; table scrolls sideways on phones.

## 0.1.10
- New **Assets** tab (Desk | Assets in the header; `#assets` opens it). Schwab-style view: account value, day change,
  unrealized gain/loss, positions (qty, price + change, market value, day change, gain/loss), Equities total,
  Cash & Money Market. Fed JSON only (no brokerage login), stale badge after 6 h, "as of" date.
- New read-only `GET /api/assets`: newest valid snapshot (by `as_of`) from `/share/trading_desk/assets.json`
  or the `assets` key of the Life HQ private feed (`/homeassistant/pyscript/.life_hq_feed.json`). Numbers are
  whitelisted and coerced; nothing is logged. No new options, no new write paths (self-check still passes).
## 0.1.9
- Whole-portfolio header at top: equity, today $/%, lifetime $/% vs net deposits, per-position day and lifetime P/L (from desk feed `portfolio`).


## 0.1.8
- Fix: 'Alpaca realized, closed trades (FIFO)' per-symbol breakdown showed raw HTML. Symbols are
  escaped and the coloured signed amounts render as HTML, in a small wrapping inline list.
- Includes all 0.1.7 changes below.

## 0.1.7
- Layout: cards size to their own content (grid align-items:start, card align-self:start) instead of
  stretching to the tallest card in the row. Fixes the empty area under Positions next to the Schwab
  holdings card; same fix applies to every card row.
- Includes all 0.1.6 changes below.

## 0.1.6
- Every signed P&L number (day change, unrealized, realized, all-time, totals, combined; $ and %)
  is coloured by one shared helper: green positive, red negative, neutral zero. Prices, shares and cash stay plain.
- Includes all 0.1.5 changes below.

## 0.1.5
- Schwab rows priced live from Alpaca market data (IEX last trade): value, unrealized, day change vs
  previous close, Schwab day total, 'price as of' time. Funds without a quote (e.g. a retirement fund) keep the snapshot price, labeled 'snap'.
- Includes all 0.1.4 changes below.

## 0.1.4
- View-only "Long-term holdings (Schwab)" card from the desk feed `holdings` block: shares,
  cost/share, value, unrealized P&L per position, Schwab + Alpaca combined total. Not counted in open risk.
- Since-day-one P&L: Avg cost column on Alpaca positions (unrealized vs avg entry next to Day);
  Alpaca total since inception (equity − net deposits), FIFO realized from fills, unrealized, other;
  Schwab unrealized vs cost; combined line.
- Includes all 0.1.3 changes below.

## 0.1.3
- Desk feed from a private GitHub repo (optional): `feed_github_repo` (owner/name),
  `feed_github_path` (default `status.json`), `feed_github_token` (password; fine-grained PAT,
  read-only Contents on that repo), `feed_github_poll_s` (>= 60, default 60). Read-only GET of the
  contents API with ETag caching (304s are free); keeps the last good document on errors; falls back
  to status_file / custom_templates / share when GitHub has nothing. The token is never logged.
- Includes all 0.1.2 changes below.

## 0.1.2
- No more invented data in live mode: without a desk feed the built-in EXAMPLE document is
  no longer shown (mock mode only). Bots the feed doesn't cover show "no feed"; header and
  Desk bots badge show feed state live / stale (>15 min) / missing.
- Execution Trader and Risk Manager tiles are computed from Alpaca (last trade event or fill,
  open-order count; open risk vs cap, unprotected positions) when the feed lacks them ("auto" tag).
- Room (was "preview"): today's Alpaca fills are drawn as hand-offs from Execution Trader to a
  Book shelf, merged with feed pipeline events; new fills flash the desk; respects reduced motion.
- Price chart (was a close-only line): 5-min candles + VWAP, entry and resting-stop lines,
  live last-trade tick folded into the newest candle, bars re-fetched every 60 s.
- Thresholds (was a text list): gauges with breach colouring for open risk vs cap, largest
  name vs cap, unprotected positions, day P&L, plus any numeric feed threshold (`current` + `limit`).
- New "Open orders & stops" table (GET /v2/orders data already fetched).
- Status file search order now matches the spec: HA config custom_templates first, then /share.

## 0.1.1
- Build: copy the whole build context and install rootfs/ from it; if rootfs/ is missing,
  the build log lists what the builder received (first local build on HA failed with
  `COPY rootfs /: "/rootfs": not found`).

## 0.1.0
- Phase 1 MVP: trade_updates + market-data websockets with backoff/jitter, heartbeat,
  REST resync on (re)connect; account, day P&L, positions with live P&L, day equity curve,
  today's fills, risk vs caps, desk bot tiles, rotation proposals, connection health.
- Phase 2 preview: hand-off room, per-symbol price chart, thresholds panel.
