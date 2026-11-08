# Home Assistant add-ons

Home Assistant add-on repository.

| add-on | |
|---|---|
| [Trading Desk](trading_desk/) | Read-only live control room for an Alpaca equity desk (Ingress panel) |
| [CallCatch Voice](twilio_house_voice/) | Twilio Media Streams bridge to xAI Realtime, with caller audio saved for CallCatch |
| [CallCatch](voicemail/) | Your Personalized Voicemail. iPhone-style house inbox (Ingress panel) |
| [Smart HVAC](smart_hvac/) | Smart thermostat for multi-zone mini-split systems: all heads share one outdoor unit, so they always stay in the same heat/cool mode. Presence/weather/sun-aware, plus radiant floor (Ingress panel "Climate"). Installs **off + dry run**. |
| [Tesla Smart Charge](tesla_smart_charge/) | Lowest-amp off-peak Tesla charging that hits your target by departure. BLE first with cloud fallback; charge-now, pause and trip-mode helpers. Installs in **dry run**. |
| [Social Pulse](social_pulse/) | Read-only X engagement plus per-site Cloudflare edge traffic and sampled Web Analytics (Ingress panel). Tokens are password options only. |
| [Sports News](sports_news/) | Personal Seahawks, Navy, and Gonzaga snapshot (Ingress panel). Does not call X. Parent publishes a JSON file. |
| [Usage](usage_dashboard/) | Read-only weekly Grok Bot usage snapshot (Ingress panel "Usage"). Canonical source: https://github.com/bcart180/ha-usage-dashboard |

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

# Sports News (stdlib only, labeled starter snapshot):
python3 sports_news/tests/test_sports_news.py

# Usage (stdlib only, bundled snapshot):
python3 usage_dashboard/tests/test_usage_dashboard.py
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

`archive/spelling/`: Shelved 2026-10-11, the owner chose LAN-only; not installed.
