# BRYSON-UI-4 Charcoal — shared design tokens

One source of truth for every Bryson page: HA theme, Lovelace custom cards, add-on UIs, the spelling app and standalone HTML.

- **Edit only `tokens.json`.** Then run `python3 shared/design/build.py` and `python3 shared/design/sync.py`.
- CI (`.github/workflows/design-tokens.yml`) fails if a generated file or a vendored copy is stale.
- Design authority: Bryson's dark Tesla component references (BRYSON-UI-4, 2026-11-10), modified 2026-11-25 22:41 PT (Bryson): black page background #000000; cards, tiles, fonts, text and styling per BRYSON-UI-4.
- Rules: black page background, charcoal cards; no orange/amber (errors use `danger`); `accent` #3E6AE1 for fills, `accent-text` #6D8EE8 for blue text/icons on surfaces; real bulb colors are never overridden; 24-hour time (`time.intl` → `hourCycle: "h23"`); WCAG AA.

## Generated files (do not hand-edit)

| File | Used by |
|---|---|
| `bryson-ui-4.css` | Custom properties only (`--bui-*`). Link from every HTML page / add-on. No element styles, so linking it never changes an existing layout. |
| `bryson-ui-4-base.css` | Optional element/component layer (`.bui-card`, `.bui-btn`, `.bui-tabs`, inputs, headings). New pages only. |
| `ha-theme.yaml` | Home Assistant theme **BRYSON-UI-4 Charcoal** (`/config/themes/bryson_ui_4_charcoal.yaml`). Same tokens at top level and under `modes.dark` so HA renders dark control fills. |
| `tokens.mjs` | ES module (`BUI`, `BUI_CSS`, `BUI_TIME`) for Lovelace custom cards. |

## Core tokens and contrast (WCAG 2.x)

| Token | Value | On page #000000 | On surface #2a2a2a | On raised #393939 |
|---|---|---|---|---|
| text | #f5f5f5 | 19.26 | 13.17 | 10.59 |
| text-secondary | #c8c8c8 | 12.55 | 8.58 | 6.90 |
| text-disabled | #8a8a8a | 6.08 | 4.16 | 3.35 |
| accent-text | #6D8EE8 | 6.68 | 4.57 | 3.68 |
| danger | #ff5d73 | 7.07 | 4.83 | 3.89 |
| success | #3ddc84 | 11.77 | 8.04 | 6.47 |
| heat | #ff6b9a | 7.82 | 5.35 | 4.30 |
| cool | #5aa2ff | 8.05 | 5.50 | 4.43 |
| accent (fill) | #3E6AE1 | 4.35 (UI ≥3) | 2.98 | — |

White text on accent fill: 4.82. Use `accent` as a fill/indicator, `accent-text` for blue words and icons on cards.

## How each page consumes it

| Page | Where it lives | How it consumes the tokens |
|---|---|---|
| HA frontend (all Lovelace dashboards) | `/config/themes/bryson_ui_4_charcoal.yaml` | Copy of `ha-theme.yaml`; default + default dark theme. |
| Add-on ingress UIs (Trading Desk, Social Pulse, Usage, Tesla Smart Charge, Smart HVAC, Sports) | `<addon>/rootfs/app/static/` | `sync.py` vendors `bryson-ui-4.css` to `static/bui/` (add-on build contexts can't reach `shared/`). Page `:root` names become aliases (`--bg: var(--bui-page)` …); the rest of each stylesheet stays layout. Trading Desk is migrated on this branch as the reference. |
| Homebase (`bryson-ui-2` card), Echo bedroom card, `smart-hvac-card.js`, School/Budget/Finance cards | `/config/www/...` (Lovelace resources) | Serve one copy at `/local/bryson-ui/tokens.mjs` + `/local/bryson-ui/bryson-ui-4.css`; cards `import { BUI_CSS } from '/local/bryson-ui/tokens.mjs'` and put `BUI_CSS` on `:host`, replacing hard-coded `SURFACES`/hex. Needs Bryson's OK (writes to /config/www). |
| thermostat.html, projects/active.html, life_hq/index.html | `/config/www/...` | `<link rel="stylesheet" href="/local/bryson-ui/bryson-ui-4.css">` + alias local vars. Needs Bryson's OK. |
| Spelling (kids) | `/share/spelling/spelling.html` | Same link (served copy) for surfaces/text; keeps its own extra kid colors on top. Word lists untouched. |
| CallCatch | — | On hold; not migrated. |
