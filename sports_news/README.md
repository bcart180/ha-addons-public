# Sports News

Personal sidebar panel for Seattle Seahawks football, Navy Midshipmen football, and Gonzaga Bulldogs men's basketball. Pure-black page (`#000000`), Trading Desk reading size, 16px cards. Seahawks are purple, Navy orange, Gonzaga pink. Yellow, red, green, and cyan mark stale, error, official, and reporter labels. The words are always there with the color.

The panel does not call X, does not use a bearer token, and does not schedule a collector. It reads a snapshot. The page checks that file every 60 seconds. That check is not a new collection.

Until a snapshot is published, the panel shows the starter collection from Nov 8, 2026, 4:50 AM ET and says it is not a live feed.

## Publish

A parent writes one complete JSON document with the Home Assistant file tool `ha_write_file`. Preferred path:

`custom_templates/sports_news/inbox.json`

The add-on reads that file at `/homeassistant/custom_templates/sports_news/inbox.json` or `/config/custom_templates/sports_news/inbox.json`. Alternate path: `/share/sports_news/inbox.json`.

Do not write it under `www/`. That folder is web-public. This add-on does not read `www/`.

No host port is opened. There is no write URL. Ingress is the only page, and it accepts GET and HEAD only.

See [DOCS.md](DOCS.md) for the schema and [SECURITY_REVIEW.md](SECURITY_REVIEW.md) before install.

## Proposed collector

Not scheduled by this add-on. A parent may collect with Grok built-in public X reads: every 30 minutes from 7:00 AM to 11:00 PM ET, and hourly from 11:00 PM to 7:00 AM ET. A 10-minute read during a game is a parent choice for a game they already named. This add-on does not detect games.

Allowlist: `@Seahawks`, `@NavyFB`, `@ZagMBB`, `@TheoLawson_SR`, `@BWagner_CapGaz`. `@bcondotta` stays off until a publisher page crosslinks the handle.
