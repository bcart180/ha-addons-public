# Changelog

## 0.1.0 — 2026-11-08

- First version. Read-only ingress panel for Seattle Seahawks football, Navy football, and Gonzaga men's basketball.
- Serves the labeled starter snapshot collected Nov 8, 2026, 4:50 AM ET until a parent publishes a newer file. That starter is not a live feed.
- Publish contract is a JSON file written with the Home Assistant file tool. The add-on does not call X and has no token.
- Invalid publishes keep the last good snapshot and say so. Stale, empty, and source-error states are explicit.
