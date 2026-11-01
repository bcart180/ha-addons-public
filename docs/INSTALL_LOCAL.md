# Install Trading Desk as a local add-on (private repo route)

The repo stays private, so Supervisor can't pull it. Install it as a **local add-on** from `/addons`.

## What's staged (non-public)
Staged 2026-10-11 10:55 AM ET from repo head `cde40fd53dd02ffba022bd5feef04751998f07b5`:

- `/share/trading_desk_install/trading_desk/`: unpacked add-on, 15 files
- `/share/trading_desk_install/trading_desk-0.1.0.zip`: same 15 files under `trading_desk/`
  (sha256 `46731e82d6508c0a2fb622f7d97bfbe1d38a80135f313af7218f98e1501d8dce`)
- `/share/trading_desk_install/MANIFEST.sha256`: sha256 of every file. All 15 matched the source,
  the /share copy and the zip, and the source matched the git blob SHAs at `cde40fd5`.
- Source copy: `/config/custom_templates/trading_desk_install/` (left in place, nothing deleted).

Nothing is in `www/`. HA core can't see `/addons` (its mounts are /config, /share, /ssl, /media),
so a person has to do the copy.

## Which tool works
- **Studio Code Server**: yes. It maps `addons` (rw) at `/addons` and `share` at `/share`, so no config change is needed.
- Advanced SSH & Web Terminal: also maps `addons:rw` and `share:rw`. Use it as a fallback.
- Samba: the `addons` share works too (copy the folder from the `share` share into `addons`).

## Steps (Studio Code Server)
1. Sidebar → Studio Code Server → ☰ → Terminal → New Terminal.
2. Run:
   ```
   mkdir -p /addons/trading_desk && cp -r /share/trading_desk_install/trading_desk/. /addons/trading_desk/
   cd /addons/trading_desk && sha256sum -c /share/trading_desk_install/MANIFEST.sha256
   ```
   You should see 15 lines ending in `OK`.
3. Settings → Add-ons → Add-on Store → ⋮ (top right) → **Check for updates** (this re-scans local add-ons).
4. Scroll to **Local add-ons** → **Trading Desk** → **Install**. It builds on the HA box and takes a few minutes. No HA restart.

## Keys and config
1. Alpaca dashboard (LIVE account) → API keys → generate a **new, dedicated, read-only** key.
   Set **Trading permissions: Read only** (required). Do not paste a full-access key into the add-on.
   `trading_env: live` only chooses the live host; trade rights come from the key's permissions.
2. Trading Desk → **Configuration**: `api_key_id`, `api_secret_key`; optional `temp_open_risk_cap_pct: 20`,
   `single_name_cap_pct: 10`; `data_feed: iex`; `trading_env: live`. Optional fields sit under "Show unused optional configuration options".
3. **Save** → Info tab → **Start** → turn on **Show in sidebar**.
4. Open Trading Desk. The top health bar should show **Trades streaming** and **Data/IEX streaming**
   (green), and **Resync** should be a few seconds old. Off-hours, Data can sit quiet, which is normal.
   If Data shows error 406, another client already holds Alpaca's one data connection. Prices then come from REST every 15 s.

## 0.1.1 re-copy (first HA build failed)

First install on HA (Oct 11, 11:13 AM ET) failed at `COPY rootfs /` with
`"/rootfs": not found` (the builder received no `rootfs/`). 0.1.1 copies the whole
build context and, if `rootfs/` is still missing, prints what the builder received.

Staged (non-public): `/share/trading_desk_install/v0.1.1/trading_desk/` (+ zip, MANIFEST.sha256).
In the Studio Code Server terminal:

    ls -la /addons/trading_desk /addons/trading_desk/rootfs/app   # paste output if the build fails again
    cp -r /share/trading_desk_install/v0.1.1/trading_desk/. /addons/trading_desk/
    cd /addons/trading_desk && sha256sum -c /share/trading_desk_install/v0.1.1/MANIFEST.sha256

Then the store reload + install is done from the agent side (or Add-on Store, Check for updates, Trading Desk, Install).
