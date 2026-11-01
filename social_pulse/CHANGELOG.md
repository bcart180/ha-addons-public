# Changelog

## 0.1.2
- Public-safe defaults: `x_accounts` and `zones` ship empty, and `cloudflare_account_id` has no default. Set them in the Configuration tab. Test fixtures use example domains and obviously fake IDs. No code change. Existing installs keep their saved options.

## 0.1.1
- Ingress source allowlist: only the Supervisor ingress proxy (172.30.32.2) is served; every other source gets 403 (MA L1).
- selfcheck now AST-scans for implicit POSTs (`Request(data=...)`, positional body, `urlopen(data=...)`), single- or double-quoted write methods, and 40-char Cloudflare-shaped tokens regardless of quoting (MA L2). Tests for each.

## 0.1.0
- First version. Read-only X public metrics (followers, posts, per-post impressions, likes, reposts, replies, quotes, bookmarks) for each configured handle.
- Cloudflare zone analytics (requests, page views, unique visitors, bandwidth, threats, countries, top paths) and Web Analytics page loads/visits, 7 and 30 days.
- Any metric the tokens can't read is shown as "not available" with the reason. Nothing is estimated.
