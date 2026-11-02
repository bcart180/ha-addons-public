# Social Pulse

Read-only sidebar panel: X engagement for your accounts and Cloudflare traffic for your sites. Soft Slate house theme.

## Tokens (enter in Configuration; both are password fields, stored only in the add-on's /data/options.json)

### `cloudflare_api_token`
Create at dash.cloudflare.com → My Profile → API Tokens → Create Custom Token:
- **Zone → Analytics → Read**, Zone Resources: *Include → Specific zone →* `site-a.example` and `site-b.example` only.
- **Account → Account Analytics → Read** on your account (needed only for Web Analytics page loads/referrers). Leave it out and those tiles show "not available".
- Nothing else: no Edit permissions, no Zone:Read, no Account Settings. Zone ids and Web Analytics site tags are plain config options, so the token never has to list anything.

### `x_bearer_token`
An X API v2 **app-only bearer token** (developer console → your app → Keys and tokens → Bearer Token). It is read-only by nature: it can't post, like or follow. It reads public profile metrics and each post's public metrics (impressions, likes, reposts, replies, quotes, bookmarks).
Profile visits, link clicks and account-level impressions need each account's own OAuth 2.0 user-context login (`tweet.read users.read` + owner-only metrics). This add-on does not do that, and shows those as **not available**.

## Options
| option | meaning |
|---|---|
| `x_accounts` | one handle per line. Add `- example_handle` to include @example_handle. |
| `zones` | name, zone_id, optional `rum_site_tag` (Web Analytics) per site |
| `cloudflare_account_id` | account for Web Analytics queries |
| `refresh_minutes` | 15–1440, default 30 |
| `snapshot_file` | dev only: serve a saved snapshot instead of calling APIs |

## Data sources
| metric | source / endpoint | available | note |
|---|---|---|---|
| Followers, following, total posts | X `GET /2/users/by?usernames=` public_metrics | yes | |
| Posts in last 7d, per-post impressions, likes, reposts, replies, quotes, bookmarks | X `GET /2/users/:id/tweets` public_metrics | yes | impressions are X's public view count |
| Engagements / engagement rate (7d) | computed from the above | yes | likes+reposts+replies ÷ impressions |
| Profile visits, link clicks | X non_public/organic_metrics | **no** | owner user-context only (X returns "not-authorized-for-field") |
| Account-level impressions | — | **no** | no X endpoint; panel sums post impressions instead and labels it |
| Requests, page views, bandwidth, threats, daily uniques (7/30d) | CF GraphQL `httpRequests1dGroups` | yes | edge counts include bots; uniques are summed daily uniques |
| Countries (7d) | CF GraphQL `httpRequests1dGroups.sum.countryMap` | yes | |
| Top pages (7d) | CF GraphQL `httpRequestsAdaptiveGroups` by `clientRequestPath` | yes | assets, /api and WordPress probes filtered out |
| Real page loads, visits, referrers, pages (30d) | CF GraphQL `rumPageloadEventsAdaptiveGroups` (Web Analytics) | yes | sampled; needs Account Analytics:Read |

## Safety
No host ports (Ingress only), no Home Assistant API, no /config, /share or /config/www mapping. The server refuses POST/PUT/PATCH/DELETE. `selfcheck.py` fails the image build on any write method/endpoint or embedded token. Errors are redacted before logging.
