# Sports News security review — before install

Status: review packet for the branch. Nothing in this change is installed, restarted, or merged. Trading Desk was not modified. Social Pulse collector, options, and read-only server were not modified. Spelling and Homebase were not modified.

## Decision

Personal sports page in the existing add-on repository, new slug `sports_news`, same ingress pattern as Social Pulse. The page is admin-only (`panel_admin: true`). No host `ports:` key. `homeassistant_api: false`. Mounts are `homeassistant_config:ro` and `share:ro`.

The add-on does not fetch X. It does not contain a bearer token, an API key, or an X client. A parent publishes a snapshot with the existing Home Assistant file tool. This packet does not create that routine.

## Routes and methods

| route | method | result |
|---|---|---|
| `/`, `/index.html`, `/app.css`, `/app.js` | GET, HEAD | static files from a fixed map |
| `/api/health` | GET, HEAD | `{"ok": true}` |
| `/api/snapshot` | GET, HEAD | validated view |
| any other path | GET, HEAD | 404 |
| POST, PUT, PATCH, DELETE, OPTIONS | any allowed client | 405 |
| any method from a non-allowlisted address | | 403 |

Default allowlist is `172.30.32.2` (Supervisor ingress proxy and watchdog). `SN_ALLOW` exists so tests can bind localhost. It is not a host-published port.

## Snapshot ingestion

First existing file wins:

1. `/homeassistant/custom_templates/sports_news/inbox.json`
2. `/config/custom_templates/sports_news/inbox.json`
3. `/share/sports_news/inbox.json`

`inbox_file` may select one of those three paths only. Any other option value is refused. The resolved path must stay under those directories, so a symlink that leaves the directory is not read.

Checks before a file becomes current: UTF-8, 262144 byte cap, duplicate JSON keys rejected, schema version 1, timezone, allowlisted authors, pinned ids where the Nov 8 starter supplied them, https X status URLs only, plain text with no `<` or `>`, `published_at <= collected_at`, collection not more than 5 minutes ahead of the add-on clock, 75 post cap.

A valid file is stored with an atomic replace into `/data/last_good.json` (temp file, fsync, `os.replace`, mode 600). An invalid file or an error document with zero posts does not replace that file. The page keeps the last good cards and says the collection time was not reset. There is no silent empty.

If nothing valid has been published, the page serves `preview/starter_2026-11-08T095038Z.json` and labels it "Starter snapshot collected Nov 8, 2026, 4:50 AM ET. Not a live feed." The filename carries the collection time. It is not written into last-good just by being shown.

`www/` is not read. A publish into `www/` would be a public HA static file and is out of contract.

## Browser

Content-Security-Policy: `default-src 'self'`; script, style, and connect are `self`; images, fonts, and frames are none; `base-uri` and `form-action` are none; `frame-ancestors 'self'`. No `unsafe-inline`. No third-party script, embed, or analytics.

The page builds cards with `textContent`. A post link is an anchor only when both the server and the page accept `https://x.com/{handle}/status/{id}` or the same path on `twitter.com`. `rel` is `noopener noreferrer` and referrer policy is `no-referrer`. `t.co` links inside post text stay text.

The page polls `api/snapshot` every 60 seconds. The response says that poll is not a collection. `collected_at` changes only when a new valid snapshot says so.

Colors are category labels (team, official, reporter, stale, error, older). Each state also has words. Focus is a 2px white outline. Team and view controls are at least 44px.

## Secrets and logs

No secrets in source, frontend, or the starter file. Request logging is off so ingress tokens are not written. Snapshot failures log a fixed phrase, not the post body. Author ids are checked and are not copied into the JSON the browser receives. `evidence_files` are not copied either.

## What is not claimed

No refresh routine is scheduled. The proposed cadence (30 minutes from 7:00 AM to 11:00 PM ET, hourly overnight, stale after two missed cycles) is a label. A parent may later use built-in public X reads and `ha_write_file`. Direct X API, copied bearer tokens, and browser login are not part of this add-on.

Social Pulse 0.3.1 is presentation only: pure-black page and full-spectrum category colors. Its collector and deny-write server are unchanged.

Trading Desk stays 0.3.2. Its near-black canvas and chart colors, including the small Bitcoin orange in the allocation chart, are untouched.

## Residual risks

- Whoever can write the inbox path can publish text the allowlist accepts. That is the HA admin file tool, not an anonymous port.
- ZagMBB and BWagner_CapGaz numeric ids are not pinned because they were not in the starter file. A mismatched id on those two handles would be accepted if the handle and role match. Seahawks, NavyFB, and TheoLawson_SR ids are pinned.
- `ha_write_file` is not an atomic rename. A half-written file fails JSON checks and leaves the last good snapshot in place.
- The starter posts are real public posts from that collection time. They go stale on the clock. They must not be described as current after the stale line appears.
- Install is still a Supervisor app update. This review is the pre-install packet, not approval to install.

## Rollback

Do not install `sports_news` and the panel does not exist. Social Pulse rollback is staying on 0.3.0; 0.3.1 changes CSS and chart colors only.

## Tests

`python3 sports_news/rootfs/app/selfcheck.py` and `python3 sports_news/tests/test_sports_news.py` cover the starter label, URL and handle rejection, stale versus older, last-good retention, empty success, symlink escape, ingress 403, write 405, and CSP. Social Pulse tests still pass, including the read-only selfcheck.
