# Sports News

Personal sidebar panel for Seattle Seahawks football, Navy Midshipmen football, and Gonzaga Bulldogs men's basketball. Pure-black page (`#000000`), Trading Desk reading size, 16px cards. Seahawks are purple, Navy orange, Gonzaga pink. Yellow, red, green, and cyan mark stale, error, official, and reporter labels. The words are always there with the color.

The panel does not call X, does not use a bearer token, and does not schedule a collector. It does not mount the Home Assistant config directory or the shared folder. The installed page shows the starter collection from Nov 8, 2026, 4:50 AM ET and says it is not a live feed.

Publishing uses one configuration option, `snapshot_json`. It is public JSON text, at most 262144 characters. The labeled starter file is 8791 bytes, and a raw snapshot around 15KB fits. Supervisor checks the length and writes `/data/options.json` only when this app starts. Saving the option does not update the running page. A parent updates only that option, reads it back, then restarts only Sports News. This add-on does not mount a host folder and does not call the Supervisor API. See [DOCS.md](DOCS.md) and [SECURITY_REVIEW.md](SECURITY_REVIEW.md).

No host port is opened. There is no write URL. Ingress is the only page, and it accepts GET and HEAD only. `panel_admin` hides the sidebar entry from non-admins. It is not a per-request admin check inside this process. The process allows only the Supervisor ingress proxy address.

The snapshot rules (allowlist, stale states, last-good retention) run on `snapshot_json` at startup. They are not connected to a host directory.

## Proposed collector

Not scheduled by this add-on. A parent may collect with Grok built-in public X reads: every 30 minutes from 7:00 AM to 11:00 PM ET, and hourly from 11:00 PM to 7:00 AM ET. A 10-minute read during a game is a parent choice for a game they already named. This add-on does not detect games.

Allowlist: `@Seahawks`, `@NavyFB`, `@ZagMBB`, `@TheoLawson_SR`, `@BWagner_CapGaz`. `@bcondotta` stays off until a publisher page crosslinks the handle.
