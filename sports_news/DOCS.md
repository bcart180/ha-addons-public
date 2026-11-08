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
| ZagMBB | Gonzaga Bulldogs | official | 602989093 |
| TheoLawson_SR | Gonzaga Bulldogs | reporter | 291955806 |
| BWagner_CapGaz | Navy Midshipmen | reporter | 872205181 |

`bcondotta` is rejected. Any other handle is rejected. A different numeric id is rejected for every allowlisted handle. Official posts must use `source_provenance: official`. Reporter posts must use `reporter`. `rumor` and any other provenance are rejected. A reporter summary is shown as that reporter's post, not as a house fact check. The ids for ZagMBB and BWagner_CapGaz come from the Nov 8 public-profile payload, not from the starter post list.

## Publish contract

`snapshot_json` is one string option. The schema is `str(0,262144)`, which the Supervisor option parser accepts as a character length (`supervisor/apps/options.py`, `vol.Length`). It rejects a longer value. It does not truncate. The Supervisor HTTP API accepts a body up to 16 MiB (`MAX_CLIENT_SIZE` in `supervisor/api/__init__.py`). The starter snapshot is 8791 bytes. A snapshot around 15KB is inside both limits. This app also rejects more than 262144 UTF-8 bytes.

The default is an empty string. Until the first valid value is saved and Sports News is restarted, the page shows the starter snapshot and says it is not a live feed.

Official options behavior: Supervisor writes `/data/options.json` when the app starts, not while it is running. Source: https://contributors.home-assistant.io/apps/guides/options/ . This app reads that file once at startup and runs the same snapshot validator used for every other snapshot. A page poll does not read the option again.

A parent, after this app is installed, uses the existing app tool. This repository does not call it.

1. Find the installed slug with `ha_get_app`. Other apps from this store were seen with the prefix `a1d3bd4b_`. Do not assume the prefix. Use the slug that tool returns.
2. Update only this option: `ha_manage_app` with that slug and `options` set to `{"snapshot_json": "<json text>"}`. The tool merges top-level keys, so `log_level` can stay unset in that call. The value must be a string, not a nested object.
3. Read the app back and confirm `snapshot_json` is the text that was sent.
4. Restart only that slug: `ha_manage_app` with `action` `restart`. Do not restart Home Assistant or any other app.

During the restart the panel is unavailable. This app does not set `timeout`, so Supervisor's default stop wait is 10 seconds (the documented default, range 10 to 300). The new process then reads the option and validates it before it listens. That gap was not measured on a live restart.

If the string is invalid JSON, too large, older than the saved snapshot, or an error document, the process keeps `/data/last_good.json` and stays up. `/api/health` still returns ok, so the watchdog does not treat bad news data as a crash. The page names the rejection and keeps the last valid collection time.

These rules still apply:

- A missing inbox after a successful save keeps `/data/last_good.json` and reports that the inbox file is missing. It does not return to the starter.
- `collected_at` older than the saved snapshot is rejected.
- `status: error` does not replace the saved snapshot. With no saved snapshot, the error is shown and is not stored as a success.
- `status: partial` with a team `status: error` keeps that team's previous posts and that team's success time. Other teams can update. The page state is error, not fresh.
- Omitting a team is not a successful empty. An explicit team `status: empty`, or a document `status: empty`, clears that team.
- A team `last_success_at` is kept on the saved document. The page shows it per team.
- The inbox open does not follow symlinks. An allowed directory that is itself a symlink is rejected.
- Identical saved bytes are not rewritten on the 60-second page check.
- The merged document is checked again before it is saved: at most 75 posts, at most 75 posts for one team, and at most 262144 bytes, plus the same author, URL, and time rules. If that check fails, the previous saved file is left byte for byte and the page reports the rejection instead of the starter.

## Document rules

`schema_version` 1. `display_timezone` must be `America/New_York`. `collected_at` is the collection time, ISO-8601 with a timezone, and not more than 5 minutes in the future. `published_at` on every post must be at or before that post's `collected_at`. A post `collected_at` must not be after the document `collected_at`. JSON checks do not prove a post is true, that the display name is the account's name, or that the URL still exists.

`status` is one of `starter_snapshot_not_live`, `ok`, `empty`, `partial`, `error`.

- `ok` and `starter_snapshot_not_live` need at least one post.
- `empty` needs zero posts.
- `error` needs `source_error` text. It does not replace a saved snapshot, whether or not it includes posts. With no saved snapshot, those posts can be shown as an error and are not stored as a success.
- `partial` needs `teams` with at least one team `status: error`.

Optional `last_attempt_at`. Team `last_success_at` is the success time for that team. A failed team does not receive the new document time. Optional `query_end_time` must be within 14 days before collection and not more than 2 minutes after it. `warnings` is a list of plain strings, required as a key (it may be empty).

Each post needs `id` (numeric status id string), `team`, `sport`, `canonical_post_url`, `author_handle`, `author_name`, `author_id` (string, not a JSON number), `published_at`, `collected_at`, `summary`, `source_provenance`, `source_text`, and `status` (`fresh` or `older`). The URL must be `https://x.com/{handle}/status/{id}` or the same path on `twitter.com`, with no query, fragment, user, or port. `source_text` is plain text, 4000 characters max. `<` and `>` are rejected. The whole file must be UTF-8 JSON at most 262144 bytes and at most 75 posts.

`evidence_files`, when present, may only be `raw/<simple name>`. They are checked and then dropped. The page does not serve them.

This repository does not call a publish tool and does not create a routine.

## Colors

Background is `#000000`. Body text is `#e8eef4` on `#171a1e` cards with a `#262b31` border and 16px radius. Team headings and counts use purple, orange, and pink. Stale and Older news use yellow words. Source error uses red words. Official uses green words. Reporter uses cyan words. The selected team control is a light pill with black text, and the button label is the team. Focus is a 2px white outline. Controls are at least 44px.

## Safety

Ingress source allowlist is `172.30.32.2`. Other clients get 403. POST, PUT, PATCH, and DELETE get 405. Content-Security-Policy is `self` only, with no inline script and no images. Links are built with `textContent` and opened with `noopener` and `noreferrer` only after a second URL check in the page. No embeds and no analytics.
