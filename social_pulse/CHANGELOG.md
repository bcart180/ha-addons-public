# Changelog

## 0.3.0 — 2026-11-06

- Per-site cards for each configured zone, including winchestergetaway.com and hideawaytavernlcv.com. A missing zone or token stays an empty state. No estimated numbers.
- Edge HTTP metrics stay on the existing zone query. Sampled visits and page loads use Web Analytics. The panel labels which is which.
- Summed daily uniques are labeled as summed, not deduplicated across 7 or 30 days.
- Sampled visits (`rum_visits_7d` / `rum_visits_30d`) are shown. An optional 7-day Web Analytics query supplies that range; if it fails, 7-day totals fall back to the 30-day daily sample and 7-day page/referrer lists stay unavailable.
- A missing Account Analytics:Read permission leaves sampled tiles unavailable and keeps edge numbers. The copy asks to add that permission on the existing token. It does not say Web Analytics is off, and it does not ask for a new token.
- 7-day share of the fetched 30-day window is shown when both totals exist. A change versus the previous 7 days is shown only when daily edge history covers that earlier window.

## 0.2.1 — 2026-11-02

- Remove '(new)' name suffix; no functional change.

## 0.2.0 — 2026-11-01

- Public store marker: name suffix (new); no functional change.

## 0.1.2
- Public-safe defaults: `x_accounts` and `zones` ship empty, and `cloudflare_account_id` has no default. Set them in the Configuration tab. Test fixtures use example domains and obviously fake IDs. No code change. Existing installs keep their saved options.

## 0.1.1
- Ingress source allowlist: only the Supervisor ingress proxy (172.30.32.2) is served; every other source gets 403 (MA L1).
- selfcheck now AST-scans for implicit POSTs (`Request(data=...)`, positional body, `urlopen(data=...)`), single- or double-quoted write methods, and 40-char Cloudflare-shaped tokens regardless of quoting (MA L2). Tests for each.

## 0.1.0
- First version. Read-only X public metrics (followers, posts, per-post impressions, likes, reposts, replies, quotes, bookmarks) for each configured handle.
- Cloudflare zone analytics (requests, page views, unique visitors, bandwidth, threats, countries, top paths) and Web Analytics page loads/visits, 7 and 30 days.
- Any metric the tokens can't read is shown as "not available" with the reason. Nothing is estimated.
