# Changelog

## 0.1.2 — 2026-11-20

- Style only. Production page and cards are `#000000`, accent `#3e6ae1`, and the tan warning token is `#ff5d73`. The `/ui-trial` shell uses the same page and accent tokens (replacing `#101010` / `#005aff`).
- Snapshot file, re-read behavior, and routes are unchanged. There is still no usage API and no countdown.

## 0.1.1

- Trial path `/ui-trial` serves a BRYSON-UI-4 presentation of the same snapshot page.
- Production `/`, `/index.html`, `/app.css`, and `/app.js` are unchanged from 0.1.0.
- Re-read still only opens the snapshot file. There is no usage API and no countdown.

## 0.1.0

- First release. Read-only ingress panel for one weekly Grok Bot usage snapshot.
- Ships the Nov 5, 2026, 8:02 PM ET report as the bundled snapshot (schema version 1).
- If `/data/usage_snapshot.json` is present and valid, the panel reads that file and labels it as a snapshot rather than bundled.
- Re-read loads the file on disk again. There is no usage API and no countdown.
