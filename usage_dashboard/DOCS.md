# Usage

Weekly Grok Bot usage, shown as a saved snapshot. The panel does not poll Home Assistant, Grok, or any other usage API. Those endpoints are not a source for this report.

The only real source is the Grok Bot `CheckSubscriptionUsage` tool, which a bot reads in chat. Someone then writes that report into a JSON file. The panel renders the file.

## What the header is saying

The bundled report is the week of Nov 3–Nov 6 UTC, read around Nov 5, 2026, 8:02 PM ET.

- Plan Ultra. The weekly usage limit was 37.4% used, shared with other apps on the plan. Grok Bot accounted for 32.7 percentage points of that limit.
- The weekly limit resets Tuesday Nov 10, 2026 at 2:44 PM America/New_York. At the snapshot, 4 days 18 hours remained. That duration is stored in the file. The panel does not count it down.
- Bot rows are shares of all Grok Bot usage, not of the weekly limit. They are whole percents, independently rounded. A value under 1% is the string `"<1"`. It is drawn as small text. It is never stored or drawn as 0.
- The header activity split (about 60% Bot-to-Bot, 26% Chats, 14% Routines) is the source page's overall figure. It is labeled approximate. It will not match a sum of the rounded bot rows, and the panel does not "fix" either side.
- Extra usage comes from additional usage credits. The snapshot has no dollar figures and no remaining-credit balance. Do not add them.

## Where a new snapshot goes

Publish only when someone asks, or on a schedule that has already been approved. The add-on does not run a schedule and does not call `CheckSubscriptionUsage`.

1. Read the report with `CheckSubscriptionUsage` in the bot chat. Copy figures from that report. Do not estimate a number the report did not give.
2. Write `usage_snapshot.json` for schema version 1 (below). Use `"<1"` for any share under 1%. Do not write `0` for those cells.
3. Put the file in the add-on data directory. The panel reads `/data/usage_snapshot.json` unless Configuration → Snapshot file points at a different `*.json` name in that same directory.
4. Open **Usage** and press **Re-read snapshot**. That request only opens the file again. It does not contact Grok Bot, Home Assistant, or any usage API. A restart is not required.

On Home Assistant OS the data directory is on the host at:

`/mnt/data/supervisor/addons/data/<repository>_usage_dashboard/`

`<repository>` is the first 8 hex characters of SHA1 of the repository URL, lowercased, which is how Supervisor names the folder. For the URL `https://github.com/bcart180/ha-addons-public` that folder is `a1d3bd4b_usage_dashboard`. A trailing slash or a `.git` suffix is a different string and a different folder. Confirm with:

```bash
ls /mnt/data/supervisor/addons/data | grep usage_dashboard
```

On a Supervised install the same folder is under `/usr/share/hassio/addons/data/`. Copy the JSON in with the SSH add-on, Samba, or `docker cp` into `/data/usage_snapshot.json` in the Usage container.

If that file is missing, unreadable, or not schema version 1, the panel keeps serving the snapshot bundled in the image and says so. The bundled label does not disappear until a valid file is in the data directory. Replacing the bundled file means a new add-on release: edit `rootfs/app/bundled/usage_snapshot.json` in https://github.com/bcart180/ha-usage-dashboard and ship a new version. The store copy in `ha-addons-public` should match that repo.

A path outside the data directory is ignored. The panel will not open it.

## Schema version 1

The bundled file is a complete example. Fields:

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `source` | Short source name shown after the timestamp. The bundled file uses `Grok Bot weekly report`. |
| `source_tool` | Tool that produced the report. `CheckSubscriptionUsage`. |
| `plan` | Plan name, shown in the kicker. |
| `snapshot_at` | ISO-8601 time the report was read, with the offset. |
| `snapshot_label` | Exact timestamp text shown in the header. The panel does not reformat it. |
| `timezone` | IANA zone the label refers to. |
| `week.label` | `This week` |
| `week.start_date`, `week.end_date` | `YYYY-MM-DD` bounds from the report. |
| `week.range_label` | Text shown in the kicker, such as `Nov 3–Nov 6 UTC`. |
| `weekly_limit.used_percent` | Weekly limit used, 0–100. Decimals allowed. |
| `weekly_limit.shared_across_apps` | `true` when the limit is shared with other apps. |
| `weekly_limit.grok_bot_percentage_points` | Grok Bot's share of that limit, in percentage points. |
| `weekly_limit.resets_at` | ISO-8601 reset instant. |
| `weekly_limit.resets_label` | Reset text shown in the card. Not recomputed. |
| `weekly_limit.until_reset_at_snapshot` | Time remaining at the snapshot, such as `4d 18h`. Not a clock. |
| `extra_usage.comes_from` | Where extra usage comes from. |
| `extra_usage.dollar_figures_included` | Must be `false` in schema 1. |
| `activity_overall.bot_to_bot_percent`, `chats_percent`, `routines_percent` | Header activity split from the source page. |
| `activity_overall.approximate` | `true` when those header percents are approximate. The panel then prefixes them with ≈. |
| `activity_overall.note` | Shown under the activity metric. Say that bot rows are independently rounded. |
| `activities` | Exactly `["Chats", "Routines", "Bot-to-Bot"]`. |
| `bots` | One object per bot, in the order to display. |
| `bots[].name` | Bot name. Unique. |
| `bots[].share_percent` | Whole number of 1 or more, or `"<1"`. |
| `bots[].activity` | Three objects in activity order, or `null` when the report had no split. `null` is allowed only for a `"<1"` share. Each `percent` is a whole number of 1 or more, or `"<1"`. |

`"0"` is rejected. A share under 1% stays `"<1"` so the panel cannot draw it as an empty bar.

## Labels

| Situation | Header |
|---|---|
| Valid file in the data directory | `Snapshot Nov 5, 2026, 8:02 PM ET · Grok Bot weekly report` (using that file's own label and source) |
| No file yet, or the file was rejected | `Bundled snapshot · Nov 5, 2026, 8:02 PM ET · Grok Bot weekly report` |

The bundled sentence uses the bundled file's own timestamp. A rejected data file is named in a warning, and the figures on screen are the bundled ones until the file is valid.

## Panel behavior

- **Re-read snapshot** sends `GET /api/snapshot` and renders that response. There is no timer and no background refresh.
- Focus filters the bars, the flow, and the Below 1% list. The donut stays the full week.
- Ribbons use whole percents only. Under 1% is text.
- The page states that this is a saved snapshot, not a live meter.
