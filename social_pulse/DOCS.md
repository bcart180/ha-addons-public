# Social Pulse

Read-only sidebar panel. X engagement for the accounts you list, and a per-site Cloudflare card for each zone you list. The household sites are `winchestergetaway.com` and `hideawaytavernlcv.com`. Other zones appear as their own cards. Pure-black page (`#000000`) with Trading Desk reading size and 16px cards. Purple, orange, yellow, red, pink, green, and cyan mark categories and sit next to written labels.

Nothing on a card is estimated. A missing token, zone, or Web Analytics field stays empty or "Not available".

## Tokens (Configuration only; password fields, stored in the add-on's /data/options.json)

### `cloudflare_api_token`
Create at dash.cloudflare.com → My Profile → API Tokens → Create Custom Token, or edit the token this add-on already uses:

- **Zone → Analytics → Read**, limited to the zones you actually list (the two sites above). This is what fills edge requests, edge page views, summed daily uniques, bandwidth, threats, countries, and top paths. These already work when this permission is present.
- **Account → Account Analytics → Read** on the same account. This is what fills sampled page loads, sampled visits, sampled pages, and referrers. Add it to the **existing** token. The token does not need to be replaced. Web Analytics does not have to be turned on again; the site tags are already a separate option.
- Nothing else: no Edit permissions, no Zone:Read, no Account Settings. Zone ids and Web Analytics site tags are plain options, so the token never has to list them.

If Account Analytics:Read is missing, sampled tiles say Not available and name that permission. Edge tiles stay filled. The panel does not copy edge requests into the sampled tiles.

### `x_bearer_token`
An X API v2 **app-only bearer token** (developer console → your app → Keys and tokens → Bearer Token). It is read-only by nature: it can't post, like, or follow. It reads public profile metrics and each post's public metrics (impressions, likes, reposts, replies, quotes, bookmarks).
Profile visits, link clicks, and account-level impressions need each account's own OAuth 2.0 user-context login (`tweet.read users.read` plus owner-only metrics). This add-on does not do that, and shows those as **not available**.

## Options
| option | meaning |
|---|---|
| `x_accounts` | one handle per line |
| `zones` | `name`, `zone_id`, optional `rum_site_tag` per site. Use the hostname as `name` (`winchestergetaway.com`, `hideawaytavernlcv.com`). |
| `cloudflare_account_id` | account for Web Analytics queries. Edge zone analytics do not use it. |
| `refresh_minutes` | 15–1440, default 30 |
| `snapshot_file` | dev only: serve a saved snapshot instead of calling APIs |

Zone ids, the account id, and site tags are 32 hex characters. They are not secrets, but they are not stored in this repo. Defaults ship empty. Set them in the Configuration tab. A zone id that is not 32 hex is not sent to Cloudflare.

## What the website cards show

Each configured zone gets one card. The 7-day / 30-day control changes the tiles. `winchestergetaway.com` and `hideawaytavernlcv.com` also get an empty card when that site is not in the snapshot, so a missing zone is obvious. The empty card has no numbers.

### Edge (Cloudflare zone analytics)
Counted at the Cloudflare edge. Includes bots, cached assets, and probes. Not browser visits.

| metric | source | range | note |
|---|---|---|---|
| Requests | `httpRequests1dGroups.sum.requests` | calendar 7 and 30 days | every HTTP request |
| Page views | `httpRequests1dGroups.sum.pageViews` | calendar 7 and 30 days | Cloudflare's HTML class at the edge, including bots. Not a sampled visit. |
| Summed daily uniques | `httpRequests1dGroups.uniq.uniques` | calendar 7 and 30 days | each day is unique IPs for that day. The 7-day and 30-day figures **add those days**. They are **not deduplicated** across the period. |
| Bandwidth, threats | same daily groups | calendar 7 and 30 days | |
| Countries | `sum.countryMap` | the selected 7 or 30 days | edge **requests** by country, not sampled visits |
| Top pages | `httpRequestsAdaptiveGroups` by `clientRequestPath`, `requestSource: eyeball` | rolling 7 days only | assets, `/api`, and WordPress/PHP probes are hidden. The probe total is a separate edge tile. This list does not become a 30-day list. |
| 7-day share of 30 days | computed from the two edge totals | | shown only when the 30-day total is above zero |
| Change vs the previous 7 days | computed from the same daily rows | | shown only when the first returned day is on or before the start of that earlier window. Otherwise the card says the history does not cover it. A prior total of zero does not get a percent. |

The fetch asks for 30 calendar days (`date_geq` today−29 through today). There is no earlier 30-day window, so the card does not invent a month-over-month change.

### Sampled (Cloudflare Web Analytics)
Real-browser beacon events. Cloudflare samples them. They are not edge request counts. They need `rum_site_tag`, `cloudflare_account_id`, and Account Analytics:Read.

| metric | source | range | note |
|---|---|---|---|
| Page loads | `rumPageloadEventsAdaptiveGroups.count` | 7 and 30 days | sampled page-load events |
| Visits | `rumPageloadEventsAdaptiveGroups.sum.visits` | 7 and 30 days | sampled visits. Both ranges are on the card. |
| Pages | same groups by `requestPath` | the selected range | |
| Referrers | same groups by `refererHost` | the selected range | blank host is shown as `(direct)` |
| 7-day share of 30 days | the two sampled totals | | same rule as edge. Hidden when the sample is not readable. |

The 30-day query is `date_geq` today−29. The 7-day query is optional and uses `date_geq` today−6, the same calendar window as the edge daily totals. It is a second read-only `query{...}` to `/graphql`. If that call fails and the 30-day daily sample succeeded, 7-day visit and page-load **totals** are the sum of those daily rows. 7-day pages and referrers stay Not available, because a 30-day top list is not a 7-day top list. Neither fallback uses edge requests.

Sampled countries are not requested.

## X metrics
| metric | source | available | note |
|---|---|---|---|
| Followers, following, total posts | `GET /2/users/by` public_metrics | yes | |
| Posts in last 7 days, per-post impressions, likes, reposts, replies, quotes, bookmarks | `GET /2/users/:id/tweets` public_metrics | yes | impressions are X's public view count |
| Engagements / engagement rate (7 days) | computed from those posts | yes | likes + reposts + replies, divided by impressions. No impressions → not available, not a guess. |
| Profile visits, link clicks | X non-public metrics | **no** | owner user-context only |
| Account-level impressions | — | **no** | no endpoint for an app token. The card sums post impressions and says so. |

## Safety
No host ports (Ingress only). No Home Assistant API. No `/config`, `/share`, or `/config/www` mapping. The server refuses POST/PUT/PATCH/DELETE from clients. The only outbound POST is Cloudflare's read-only GraphQL query endpoint, and only from `collectors._graphql`. `selfcheck.py` fails the image build on any other write method, write endpoint, or embedded token. Zone and account ids are checked as 32 hex characters before they are placed in a query. Errors are redacted before logging. A Web Analytics failure does not drop the edge numbers for that zone.
