# Sports News

Read-only Home Assistant ingress panel. No host ports. No Home Assistant API token. No X token. The add-on never calls X.

## What the page shows

Three teams, in this order: Seattle Seahawks football, Navy Midshipmen football, Gonzaga Bulldogs basketball. Each card has the team name, Official account or Reporter, attributed, the summary, the returned post text, Published time in ET, and a View post on X link when the URL is an https `x.com` or `twitter.com` status link for that author and id.

The header says Updated with the collection time. A separate line says when the page last re-read the file. Re-reading does not change `collected_at`.

States:

| state | meaning |
|---|---|
| Starter snapshot | No publish yet, or the rejected file had no last good copy. Labeled with the Nov 8, 2026, 4:50 AM ET collection. Not a live feed. |
| Inside two proposed cycles | Last success is newer than two proposed cycles. |
| Stale | Last success is older than two proposed cycles: 60 minutes from 7:00 AM to 11:00 PM ET, 120 minutes overnight. Last good cards stay. |
| No posts | The collection succeeded and the list is empty. |
| Source error | The published file says the read failed, or the inbox file was rejected. Last good cards stay. Collection time is not reset. |
| Older news | A card whose post time is more than 72 hours before the page check. Independent of feed health. |

One team can be in error while the other teams still show their posts.

## Allowlist

| handle | team | class | author id |
|---|---|---|---|
| Seahawks | Seattle Seahawks | official | 23642374 |
| NavyFB | Navy Midshipmen | official | 2429051735 |
| ZagMBB | Gonzaga Bulldogs | official | not pinned; must be a numeric string |
| TheoLawson_SR | Gonzaga Bulldogs | reporter | 291955806 |
| BWagner_CapGaz | Navy Midshipmen | reporter | not pinned; must be a numeric string |

`bcondotta` is rejected. Any other handle is rejected. Official posts must use `source_provenance: official`. Reporter posts must use `reporter`. `rumor` and any other provenance are rejected. A reporter summary is shown as that reporter's post, not as a house fact check.

ZagMBB and BWagner_CapGaz ids were not in the Nov 8 starter file. A later public-profile read was not available here, so those two ids are not pinned. The handle still has to match.

## Publish contract

`schema_version` 1. `display_timezone` must be `America/New_York`. `collected_at` is the collection time, ISO-8601 with a timezone, and not more than 5 minutes in the future. `published_at` on every post must be at or before `collected_at`. Post `collected_at` must match the document.

`status` is one of `starter_snapshot_not_live`, `ok`, `empty`, `partial`, `error`.

- `ok` and `starter_snapshot_not_live` need at least one post.
- `empty` needs zero posts.
- `error` needs `source_error` text. Zero posts means "keep last good". Posts included are shown with the error.
- `partial` needs `teams` with at least one team `status: error`.

Optional `last_attempt_at` and `last_success_at`. If omitted on a success, both equal `collected_at`. Optional `query_end_time` must be within 14 days before collection and not more than 2 minutes after it. Optional `warnings` is a list of plain strings, required as a key (it may be empty).

Each post needs `id` (numeric status id string), `team`, `sport`, `canonical_post_url`, `author_handle`, `author_name`, `author_id` (string, not a JSON number), `published_at`, `collected_at`, `summary`, `source_provenance`, `source_text`, and `status` (`fresh` or `older`). The URL must be `https://x.com/{handle}/status/{id}` or the same path on `twitter.com`, with no query, fragment, user, or port. `source_text` is plain text, 4000 characters max. `<` and `>` are rejected. The whole file must be UTF-8 JSON at most 262144 bytes and at most 75 posts.

`evidence_files`, when present, may only be `raw/<simple name>`. They are checked and then dropped. The page does not serve them.

Write the document in one call. A partial or invalid file does not replace the last good snapshot.

Example publish, after the parent has built the document from public X reads:

- Tool: `ha_write_file`
- Path: `custom_templates/sports_news/inbox.json`
- `overwrite`: true
- `create_dirs`: true
- Content: the JSON document

Alternate absolute path: `/share/sports_news/inbox.json`.

This repository does not call that tool and does not create a routine.

## Colors

Background is `#000000`. Body text is `#e8eef4` on `#171a1e` cards with a `#262b31` border and 16px radius. Team headings and counts use purple, orange, and pink. Stale and Older news use yellow words. Source error uses red words. Official uses green words. Reporter uses cyan words. The selected team control is a light pill with black text, and the button label is the team. Focus is a 2px white outline. Controls are at least 44px.

## Safety

Ingress source allowlist is `172.30.32.2`. Other clients get 403. POST, PUT, PATCH, and DELETE get 405. Content-Security-Policy is `self` only, with no inline script and no images. Links are built with `textContent` and opened with `noopener` and `noreferrer` only after a second URL check in the page. No embeds and no analytics.
