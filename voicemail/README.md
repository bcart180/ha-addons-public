# CallCatch

Your Personalized Voicemail. iPhone-style house inbox for Home Assistant. The list and detail screens use the Soft Slate house theme (`soft-slate.css`); that file is the design system, not the product name.

- **Ingress only** (sidebar panel behind HA login). No host ports. No public webhook / Cloudflare Access.
- Inbox store under `/data`. Publishes `sensor.inbound_voicemails` (count and recent items) for dashboards and the notification blueprint. Its attributes carry masked fields only (`•••1234`); full numbers and transcripts stay in the admin panel.
- CallCatch Voice posts transcripts and audio to `POST /api/ingest` with the `ingest_token` bearer secret.
- Actions: in-app playback, optional speaker (`media_player`, blank by default), clear one / clear all, mark heard. Call back queues a pending request for a human-approved outbound call elsewhere; this add-on never dials. A secondary Open on phone link can still use `tel:`.
- Audio is stored under `/media/callcatch` or `/share/callcatch`, or in a Google Drive folder. Playback streams through this App. See [DOCS.md](DOCS.md).

## Install

1. Add this repository in **Settings → Apps → App Store → ⋮ → Repositories**  
   `https://github.com/bcart180/ha-addons-public`
2. Install **CallCatch** → Start. The sidebar label is CallCatch.
3. Set `ingest_token` (password) to a long random secret. In CallCatch Voice, set `ha_webhook_transcript` and `ha_webhook_audio` to `http://local-voicemail:8099/api/ingest` (or `http://<repohash>-voicemail:8099/api/ingest`) and `ha_webhook_token` to the same secret.
4. Leave `media_player` blank unless a speaker should play recordings. Leave `storage_backend` on `local` unless you want Google Drive. Details: [DOCS.md](DOCS.md).

## Local mock

```bash
VM_MOCK=1 VM_PORT=8099 python3 rootfs/app/server.py
# open http://127.0.0.1:8099/
```

## Security

- Never expose this App publicly.
- Supervisor ingress only; LAN HA dashboard users who can open the panel.
- Mutations require ingress session (`172.30.32.2` + `X-Remote-User-Id`); sibling add-ons cannot clear/play directly.
- Ingest requires the bearer token from `ingest_token`. The token is never logged.
- Play target `media_player` is options-only (not overridable via API body) and defaults to blank.
- Google credentials are password options. Drive uploads use the `drive.file` scope and do not create public links.
- Secrets stay in HA; this App uses `SUPERVISOR_TOKEN` only to publish the sensor and to play on a configured speaker.

## Recorder exclude (recommended)

Any signed-in Home Assistant user can read entity attributes, and the recorder keeps them in history and backups. The sensor is already masked; as defense in depth, keep it out of the recorder. The App cannot edit `configuration.yaml`, so add this yourself (or save it as a package, e.g. `packages/callcatch_recorder.yaml`) and restart Home Assistant:

```yaml
recorder:
  exclude:
    entities:
      - sensor.inbound_voicemails
```

If you changed `sensor_entity`, use that entity id instead.
