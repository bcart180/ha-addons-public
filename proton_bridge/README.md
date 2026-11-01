# Proton Mail Bridge — Home Assistant add-on (built from source)

Runs **Proton Mail Bridge v3.27.1** built from Proton's official source
(`github.com/ProtonMail/proton-bridge`, tag `v3.27.1`, commit
`43b9237786204a0215abfd4dcc51ee7bb92f52d4`) on Home Assistant OS (Raspberry Pi 5, aarch64).
No third-party images. The Dockerfile refuses to build if the tag no longer points at that commit.

> Upstream tag `v3.27.1` is a lightweight tag and the commit is **unsigned** (GitHub API
> `verification.reason = unsigned`, checked 2026-10-30). Provenance = Proton's official repo +
> pinned SHA, not a signature.

## Status
**Not installed.** Review order: President & COO (HA side) → VP, Mission Assurance → CoS → owner's install GO.
The box Bridge (`/home/box/bin/ensure-proton-bridge.sh`) stays as the **fallback** and is not touched.

## What it does / doesn't do
- Keychain (GnuPG key + `pass` store), Bridge vault, settings and cache: all under `/data`.
- Exposes **IMAP only**: host port **1143** (LAN) → container 8143 (socat) → Bridge 127.0.0.1:1143.
  SMTP stays on container localhost (127.0.0.1:1025) and is **not published**.
- No add-on options hold secrets. No HA-side alert or send path. No notifications, no automations.
- Watchdog: Supervisor probes host port 1143 (socat on container 8143). The add-on stops that socat listener and exits when Bridge's own IMAP greeting on 127.0.0.1:1143 fails, or when the Bridge process exits, so Supervisor restarts it.

## Install (only after owner's GO)
Listed in the existing store repo `bcart180/ha-addons-public` (folder `proton_bridge/`, a synced copy of
this repo). **Do not change the Supervisor store URL.** Settings → Apps → App Store → ⋮ → Check for
updates → *Proton Mail Bridge* → Install. Supervisor builds the image locally on the Pi
(Go build; expect ~10–20 min the first time). v1 is build-on-Pi on purpose; a prebuilt
multi-arch GHCR image may come later. The pinned-commit check runs on every build either way.

## First login (owner, once)
1. Configuration → `login_mode: true` → Save → Start.
2. Open the **Proton Bridge** sidebar panel (admin-only ingress; terminal running `bridge --cli`).
   ttyd runs only in login mode; in normal mode there is no ttyd process and the panel is empty.
3. Type `login`, enter Proton username, password, 2FA (typed only here; never stored in options).
4. Type `info` → note the **Bridge IMAP username + Bridge password** (not your Proton password).
5. Save those into the box file `~/.config/proton-bridge.env` yourself (or the vault). Never paste in chat/Notion/GitHub.
6. `exit`, then **turn `login_mode` off** (false) and Restart. Do not leave login mode on.
   Bridge now runs `--noninteractive`.

## /data is sensitive — encrypted HA backups REQUIRED
`/data` holds the **Bridge vault** (Proton session + Bridge IMAP password), a **passphrase-less GPG
key**, and the **pass store** that key unlocks. Anyone with a copy of `/data` has mailbox access.
- HA backups of this add-on **must be encrypted** (Settings → System → Backups → encryption key on;
  keep the key in the vault). Never keep or share an unencrypted backup.
- `backup: cold` (add-on stopped during backup for a consistent vault); `home/.cache/*` excluded.

## Network / access design
- LAN only. **No router port forwards.**
- The Grok box has no LAN route. Design: a cloudflared **TCP route** for `imap-bridge.home.example.com` (your own hostname)
  → HA IMAP 1143, protected by a Cloudflare Access **service-token** policy. Box side (token from the vault):
  `cloudflared access tcp --hostname imap-bridge.home.example.com --url 127.0.0.1:1143`
- cloudflared on HA: **to be confirmed by President & COO**; adding it is a **separate GO step**. Not built here.
- The box's own fallback Bridge also uses 127.0.0.1:1143, so only one can be bound on the box at a time
  (switching is part of that GO step).
- IMAP is STARTTLS with Bridge's self-signed cert; clients pin or trust that cert.

## Fallback
If the add-on is down, the box Bridge keeps working as today (`ensure-proton-bridge.sh`).

## Updating Bridge
Bump `BRIDGE_TAG` + `BRIDGE_SHA` in `proton_bridge/build.yaml` and `version` in `config.yaml`,
verify the new tag/commit on Proton's repo, PR → COO → MA → GO.

## CI
`.github/workflows/build.yaml` cross-builds linux/arm64 with QEMU on every PR (no push), and pushes
`ghcr.io/bcart180/aarch64-addon-proton-bridge` on main using only `GITHUB_TOKEN`.
The store listing builds locally (v1), so the private GHCR image is optional; multi-arch GHCR later.
