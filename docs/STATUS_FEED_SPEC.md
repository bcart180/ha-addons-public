# Trading Desk status feed — spec v1

The Trading Desk add-on draws the bot tiles, the hand-off room, the rotation
proposals, the thresholds panel and (if given) the risk caps from ONE small JSON
document that the desk writes. The add-on only reads it.

## Where the add-on looks (first match wins)

1. HA entity, if the add-on option `status_entity` is set (e.g. `sensor.trading_desk_status`):
   the document is the entity's `status` attribute, or all of its attributes.
2. The file in the add-on option `status_file`, if set.
3. `/config/custom_templates/trading_desk/status.json`  ← **recommended**: writable with
   the HA MCP file tools, not web-public (unlike `www/`), and HA ignores non-`.jinja` files there.
4. `/share/trading_desk/status.json`

If none exist, the add-on (0.1.2+) shows **NO DESK FEED**: bot tiles it cannot compute say
`no feed`; Execution Trader and Risk Manager are computed from Alpaca. The built-in EXAMPLE
document is used only in mock mode. A feed whose `updated` is older than 15 minutes shows **FEED STALE**. It is re-read every 5 seconds; write the file atomically
(write `status.json.tmp`, then rename) so the add-on never reads half a file.

## Schema (`trading_desk.status/v1`)

```json
{
  "schema": "trading_desk.status/v1",
  "updated": "2026-10-11T14:05:00Z",
  "bots": [
    {"name": "Desk Lead", "status": "working", "last_action": "Routed HG-20261011-01 to Risk", "ts": "2026-10-11T14:04:10Z", "detail": "optional"}
  ],
  "pipeline": [
    {"trade_id": "HG-20261011-01", "symbol": "SPY", "stage": "scan", "ts": "2026-10-11T13:40:00Z", "bot": "optional override", "note": "optional"}
  ],
  "caps": {"temp_open_risk_pct": 20, "single_name_cap_pct": 10},
  "risk": {"open_risk_pct": 6.1},
  "thresholds": [{"name": "Max risk per trade", "value": "0.5% equity"}],
  "proposals": [
    {"id": "ROT-20261011-1", "out": "QQQ", "in": "SPY", "reason": "short reason", "score": 0.62, "status": "proposed", "ts": "2026-10-11T13:00:00Z"}
  ]
}
```

| field | required | meaning |
|---|---|---|
| `updated` | yes | UTC ISO-8601 time the desk wrote the document |
| `bots[]` | yes | one entry per bot. Names shown as tiles in this order: Desk Lead, Strategist, Risk Manager, Research Analyst, Execution Trader, Trade Reviewer, Trading Floor (other names are appended). Missing bots show `unknown`. |
| `bots[].status` | yes | `working` · `idle` · `waiting` · `alert` · `blocked` · `error` (any other string shows grey) |
| `bots[].last_action` | yes | one line, plain text, no secrets |
| `bots[].ts` | yes | UTC ISO-8601 time of that action |
| `pipeline[]` | no | append one event each time a trade moves stage; keep the last ~60. Drives the hand-off lines. |
| `pipeline[].stage` | yes | `scan` · `vet` · `size` · `risk` · `fill` · `review` |
| `pipeline[].bot` | no | which desk handled the stage. Default map: scan→Strategist, vet→Research Analyst, size→Risk Manager, risk→Desk Lead, fill→Execution Trader, review→Trade Reviewer |
| `caps.temp_open_risk_pct` | no | current TEMP open-risk ceiling, % of live equity (from `risk-limits.md`). Omit → shown as **unknown**. |
| `caps.single_name_cap_pct` | no | single-name cap, % of live equity. Omit → **unknown**. |
| `risk.open_risk_pct` | no | the desk's own open-risk number, shown next to the add-on's computed one |
| `thresholds[]` | no | free-form name/value rows for the thresholds panel. Add numeric `current` and `limit` (and optional `unit`, `lower_is_breach`) to get a gauge with breach colouring, e.g. `{"name":"Daily loss","current":0.4,"limit":2,"unit":"%"}` |
| `proposals[]` | no | rotation proposals from the screen; objects as above or plain strings |
| `example` | no | `true` marks the document as example data (badge in UI). Never set it on real data. |

Add-on options `temp_open_risk_cap_pct` / `single_name_cap_pct`, when set,
override `caps` from the feed. The UI always shows where each cap came from.

## How the add-on computes risk (for the desk to cross-check)

* **Open risk $** = Σ over positions of `qty × (avg_entry − stop)` (floored at 0),
  using resting `stop` / `stop_limit` / `trailing_stop` exit orders from Alpaca.
  Shares with no resting stop are listed as **unprotected** and are not counted.
* **Open risk %** = open risk $ ÷ live equity.
* **Largest name %** = largest |market value| ÷ live equity.
