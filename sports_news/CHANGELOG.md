# Changelog

## 0.1.4 — 2026-11-08

- The page says Content collected for the snapshot time. It does not call that time a collector check.
- Last collector check stays unavailable, and automatic check status stays unknown. A schedule or a page re-read cannot make old content look newly checked.
- The starter label stays while the saved snapshot status is `starter_snapshot_not_live`. A different accepted status clears it.
- Team error text and each team's content time are unchanged.

## 0.1.3 — 2026-11-08

- A merged snapshot is checked against the post total, the per-team total, the byte cap, and the rest of the snapshot rules before it is saved. If that check fails, the previous `/data/last_good.json` bytes stay in place and the page says why. It does not fall back to the starter.

## 0.1.2 — 2026-11-08

- Publish path is the `snapshot_json` configuration option, a public JSON string of at most 262144 characters. Supervisor writes `/data/options.json` only when Sports News starts. There is still no host mount, no write endpoint, and no Supervisor API privilege.
- Empty `snapshot_json` shows the starter snapshot until the first valid publish. A bad, older, or failed option does not replace `/data/last_good.json` and does not exit, so the watchdog does not restart again because of bad data.
- The page states that a saved option is not visible until this app is restarted, and that Supervisor's default stop wait is 10 seconds. That wait was not timed on a live restart.

## 0.1.1 — 2026-11-08

- No Home Assistant config mount and no share mount. Read-only would still expose private files, and no sanctioned writer can publish into a narrower path this add-on can read. The installed panel is snapshot-only.
- Pinned ZagMBB `602989093` and BWagner_CapGaz `872205181`.
- A missing inbox keeps the saved snapshot and says the file is missing. It does not switch back to the starter.
- An older `collected_at` is rejected and does not replace the saved snapshot.
- A team marked failed keeps that team's previous posts and success time. The page does not call that team fresh.
- Inbox opens do not follow symlinks, including a symlink used as the allowed directory. Unchanged snapshots are not rewritten. The saved file is fully written and the directory is synced.

## 0.1.0 — 2026-11-08

- First version. Read-only ingress panel for Seattle Seahawks football, Navy football, and Gonzaga men's basketball.
- Serves the labeled starter snapshot collected Nov 8, 2026, 4:50 AM ET until a parent publishes a newer file. That starter is not a live feed.
- Publish contract is a JSON file written with the Home Assistant file tool. The add-on does not call X and has no token.
- Invalid publishes keep the last good snapshot and say so. Stale, empty, and source-error states are explicit.
