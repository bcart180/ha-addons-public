# Sports News

Personal sidebar panel for Seattle Seahawks football, Navy Midshipmen football, and Gonzaga Bulldogs men's basketball. Pure-black page (`#000000`), Trading Desk reading size, 16px cards. Seahawks are purple, Navy orange, Gonzaga pink. Yellow, red, green, and cyan mark stale, error, official, and reporter labels. The words are always there with the color.

The panel does not call X, does not use a bearer token, and does not schedule a collector. It does not mount the Home Assistant config directory or the shared folder. The installed page shows the starter collection from Nov 8, 2026, 4:50 AM ET and says it is not a live feed.

Publishing is blocked. The Home Assistant file tool can write under `custom_templates/`, but this add-on would have to mount the whole config directory to read that file. That directory holds secrets and auth files. Read-only access would not keep them private. The shared folder is also broad, and that writer path was not granted. No narrower sanctioned writer was found. See [SECURITY_REVIEW.md](SECURITY_REVIEW.md).

No host port is opened. There is no write URL. Ingress is the only page, and it accepts GET and HEAD only. `panel_admin` hides the sidebar entry from non-admins. It is not a per-request admin check inside this process. The process allows only the Supervisor ingress proxy address.

The snapshot rules (allowlist, stale states, last-good retention) are implemented and tested for a future narrow bridge. They are not connected to a host directory in this version.

## Proposed collector

Not scheduled by this add-on. A parent may collect with Grok built-in public X reads: every 30 minutes from 7:00 AM to 11:00 PM ET, and hourly from 11:00 PM to 7:00 AM ET. A 10-minute read during a game is a parent choice for a game they already named. This add-on does not detect games.

Allowlist: `@Seahawks`, `@NavyFB`, `@ZagMBB`, `@TheoLawson_SR`, `@BWagner_CapGaz`. `@bcondotta` stays off until a publisher page crosslinks the handle.
