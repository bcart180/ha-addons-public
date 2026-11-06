# Usage

Read-only Home Assistant sidebar panel for a weekly Grok Bot usage snapshot. Version 0.1.0.

Canonical source: https://github.com/bcart180/ha-usage-dashboard

Add-on store copy: https://github.com/bcart180/ha-addons-public/tree/main/usage_dashboard

This add-on is independent of trading_desk. It does not trade, and it does not read HVAC or any other add-on.

## What it shows

One saved snapshot. The panel labels the time and the source, for example:

`Snapshot Nov 5, 2026, 8:02 PM ET · Grok Bot weekly report`

If the add-on data directory has no snapshot yet, the same report ships inside the image and the line starts with **Bundled snapshot**.

There is no usage API. Re-read only opens the snapshot file again. The reset figure does not count down.

## Install

The Home Assistant App Store discovers this folder when the repository is https://github.com/bcart180/ha-addons-public.

Settings → Apps → App Store → ⋮ → Repositories → add that URL if it is not already there → **Usage** → Install → Start.

Ingress only. No host ports. Sidebar label **Usage**. The panel is visible to administrators, which is the Supervisor default. No token and no Home Assistant API.

## Snapshot

See [DOCS.md](DOCS.md) for the schema and how a new file is published. The file bundled with 0.1.0 is [rootfs/app/bundled/usage_snapshot.json](rootfs/app/bundled/usage_snapshot.json).

## Local preview

```bash
python3 tests/test_usage_dashboard.py
UD_ALLOW=127.0.0.1 UD_PORT=8098 python3 rootfs/app/server.py
```

Open http://127.0.0.1:8098/. Outside Home Assistant the allow list has to include your address. The image default is the Supervisor ingress proxy only (`172.30.32.2`).

## Font

Interface text uses Inter, weights 300 and 400, bundled under the SIL Open Font License. See [LICENSES/Inter-OFL.txt](LICENSES/Inter-OFL.txt).
