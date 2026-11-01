# Home Assistant add-ons

Home Assistant add-on repository.

| add-on | |
|---|---|
| [Trading Desk](trading_desk/) | Read-only live control room for an Alpaca equity desk (Ingress panel) |
| [CallCatch Voice](twilio_house_voice/) | Twilio Media Streams bridge to xAI Realtime, with caller audio saved for CallCatch |
| [CallCatch](voicemail/) | Your Personalized Voicemail. iPhone-style house inbox (Ingress panel) |
| [Smart HVAC](smart_hvac/) | Presence/weather/sun-aware mini splits + radiant floor (Ingress panel "Climate"). Source of truth: [bcart180/smart-hvac](https://github.com/bcart180/smart-hvac); this folder is a synced copy. Installs **off + dry run**. |
| [Social Pulse](social_pulse/) | Read-only X engagement + Cloudflare site traffic dashboard (Ingress panel). Source of truth: [bcart180/ha-addon-social-pulse](https://github.com/bcart180/ha-addon-social-pulse); this folder is a synced copy. Tokens are password options only. |

## Install
Settings → Apps → App Store → ⋮ → Repositories → add
`https://github.com/bcart180/ha-addons-public` → pick an app → Install → Start.

No Home Assistant Core restart is needed for these Apps.

CallCatch Voice installs from the same store entry. After it is listed: Install → Configuration (Twilio and xAI secrets, public HTTPS base) → Start. Voice webhooks use the add-on's published port, not the sidebar panel. See [twilio_house_voice/README.md](twilio_house_voice/README.md).

CallCatch is ingress-only (no host ports). Sidebar label is CallCatch. See [voicemail/README.md](voicemail/README.md) and [docs/INSTALL_VOICEMAIL.md](docs/INSTALL_VOICEMAIL.md).

Updates: Supervisor → App Store → ⋮ → Repositories → already using https://github.com/bcart180/ha-addons-public → Check for updates.

## Develop / test locally (no keys)
```
python3 -m venv venv && venv/bin/pip install aiohttp
echo '{"mock": true}' > /tmp/opts.json
cd trading_desk/rootfs/app && TD_OPTIONS=/tmp/opts.json ../../../venv/bin/python server.py
# open http://localhost:8099/
python3 trading_desk/tests/test_readonly.py

# CallCatch mock:
cd voicemail && VM_MOCK=1 VM_ALLOW= ../venv/bin/python rootfs/app/server.py

# Social Pulse (stdlib only, real-sample fixtures):
python3 social_pulse/tests/test_social_pulse.py
```

## Docs
- [Status feed spec](docs/STATUS_FEED_SPEC.md)
- [Phase 2 plan](docs/PHASE2.md)
- [CallCatch install](docs/INSTALL_VOICEMAIL.md)

## Key safety
Use a **read-only** key if the Alpaca dashboard offers one when you generate it
(Alpaca documents Read only / Custom access levels for Broker API credentials; a
standard trading account may only offer full-access keys). A full-access key can
trade. This add-on sends nothing but GET requests and refuses to build or start if
write code appears, but the key itself stays a trading credential. Store it only in
the add-on's Configuration (HA backups include it; keep backups private), and prefer
a dedicated key you can revoke on its own.

## Spelling (shelved)

`archive/spelling/`: Shelved 2026-10-11, Bryson chose LAN-only; not installed.
