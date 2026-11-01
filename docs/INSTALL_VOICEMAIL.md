# CallCatch — install

Your Personalized Voicemail. Repo: `https://github.com/bcart180/ha-addons-public` (folder `voicemail/`). The Supervisor slug stays `voicemail`.

1. Settings → Apps → App Store → ⋮ → Repositories → add `https://github.com/bcart180/ha-addons-public` if missing.
2. Check for updates → install **CallCatch** → Start.
3. Sidebar: **CallCatch** (ingress). No host ports. LAN / HA login only.

Options for a new install: set `ingest_token`, point CallCatch Voice at `http://local-voicemail:8099/api/ingest` or `http://<repohash>-voicemail:8099/api/ingest`, leave `media_player` blank, leave `storage_backend` on `local` (or configure Google Drive). `legacy_pyscript` stays `auto`. Full steps: [voicemail/DOCS.md](../voicemail/DOCS.md).

The App publishes `sensor.inbound_voicemails` itself. A Home Assistant that still has the pyscript inbox file keeps that path until `legacy_pyscript` is `off`.
