# Changelog

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
